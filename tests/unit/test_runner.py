import asyncio
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bedcheck.service import NO_CONFIRMATION, BedCheck, Invalid, Valid
from app.clock import FakeClock
from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.runner import Runner, RunnerSettings
from app.jobs.store import JobStore
from app.notify.service import JOB_FAILED, JOB_SKIPPED, JOB_STARTED, REMINDER, Notifier
from app.printer.client import PrinterError, PrinterOffline
from app.printer.events import Connected
from app.printer.models import PrinterStatus
from tests.factories import make_file, make_status, printing_status
from tests.fakes import FakePrinter, StatusHolder

SETTINGS = RunnerSettings(tz=ZoneInfo("UTC"), start_verify_seconds=5)


@dataclass
class Env:
    clock: FakeClock
    jobs: JobStore
    bed: BedCheck
    printer: FakePrinter
    holder: StatusHolder
    notifier: Notifier
    runner: Runner

    def runner_with(self, **overrides) -> Runner:
        return Runner(jobs=self.jobs, bedcheck=self.bed, printer=self.printer,
                      status=lambda: self.holder.status, notifier=self.notifier, clock=self.clock,
                      settings=replace(SETTINGS, **overrides))

    def schedule(self, minutes: float = 0, filename: str = "cube.gcode"):
        info = self.printer.files_by_name[filename]
        return self.jobs.create(
            filename=filename, file_size=info.size, file_modified=info.modified,
            scheduled_at=self.clock.now() + timedelta(minutes=minutes),
            head_map=(HeadAssignment(0, 0, "PLA", "FFFFFF"),))

    def kinds(self) -> list[str]:
        return [n.kind for n in reversed(self.notifier.latest(50))]


@pytest.fixture
async def env(db, clock) -> Env:
    holder = StatusHolder(make_status())
    printer = FakePrinter(files={"cube.gcode": make_file()})
    printer.on_start = lambda filename: setattr(holder, "status", printing_status(filename))
    bed = BedCheck(db, clock, printer, lambda: holder.status)
    bed.startup()
    await bed.handle_event(Connected(at=clock.now(), status=holder.status))
    jobs = JobStore(db, clock)
    notifier = Notifier(db, clock, telegram_enabled=False)
    environment = Env(clock, jobs, bed, printer, holder, notifier, runner=None)
    environment.runner = environment.runner_with()
    return environment


async def test_happy_path_starts_and_consumes(env):
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.STARTED
    assert env.printer.calls == [("file_info", "cube.gcode"), ("apply_head_map", [(0, 0)]),
                                 ("start_print", "cube.gcode")]
    assert await env.bed.validity() == Invalid(NO_CONFIRMATION)
    assert env.kinds() == [JOB_STARTED]


async def test_no_confirmation_skips(env):
    job = env.schedule()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.SKIPPED, "no confirmation")
    assert ("start_print", "cube.gcode") not in env.printer.calls
    assert env.kinds() == [JOB_SKIPPED]


async def test_offline_retries_within_grace_then_skips(env):
    env.bed.confirm()
    job = env.schedule()
    env.holder.status = PrinterStatus.offline()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.PENDING
    env.clock.advance(timedelta(minutes=11))
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "printer offline"


async def test_late_job_is_skipped_even_when_printer_is_back(env):
    job = env.schedule()
    env.holder.status = PrinterStatus.offline()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.PENDING
    env.clock.advance(timedelta(minutes=10, seconds=30))
    env.holder.status = make_status()
    env.bed.confirm()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.SKIPPED, "missed (more than 10 min late)")
    assert env.printer.calls == []
    assert env.kinds() == [JOB_SKIPPED]


async def test_clock_jump_never_starts_late(env):
    job = env.schedule()
    env.clock.advance(timedelta(hours=3))
    env.bed.confirm()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.SKIPPED, "missed (more than 10 min late)")
    assert ("start_print", "cube.gcode") not in env.printer.calls


async def test_job_that_turns_late_while_reading_file_is_skipped(env):
    env.bed.confirm()
    job = env.schedule()
    env.clock.advance(timedelta(minutes=9, seconds=55))
    env.printer.on_file_info = lambda filename: env.clock.advance(timedelta(seconds=20))
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.SKIPPED, "missed (more than 10 min late)")
    assert env.printer.calls == [("file_info", "cube.gcode")]


async def test_job_that_turns_late_while_mapping_heads_is_not_started(env, monkeypatch):
    map_heads = env.printer.apply_head_map

    async def slow_mapping(assignments) -> None:
        await map_heads(assignments)
        env.clock.advance(timedelta(seconds=20))

    monkeypatch.setattr(env.printer, "apply_head_map", slow_mapping)
    env.bed.confirm()
    job = env.schedule()
    env.clock.advance(timedelta(minutes=9, seconds=55))
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.FAILED, "missed (more than 10 min late)")
    assert [call[0] for call in env.printer.calls] == ["file_info", "apply_head_map"]
    assert await env.bed.validity() == Invalid(NO_CONFIRMATION)
    assert env.kinds() == [JOB_FAILED]


async def test_dry_run_changes_nothing(env):
    runner = env.runner_with(dry_run=True)
    env.bed.confirm()
    job = env.schedule()
    await runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "dry run (all checks passed)"
    assert [call[0] for call in env.printer.calls] == ["file_info"]
    assert isinstance(await env.bed.validity(), Valid)


async def test_start_error_fails_and_uses_up_confirmation(env):
    env.printer.fail_on["start_print"] = PrinterError("HTTP 500 boom")
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.FAILED, "HTTP 500 boom")
    assert await env.bed.validity() == Invalid(NO_CONFIRMATION)
    assert env.kinds() == [JOB_FAILED]


async def test_start_error_while_the_printer_starts_anyway_is_started(env, monkeypatch):
    async def start_then_time_out(filename):  # the start went through; only the answer was lost
        env.printer.calls.append(("start_print", filename))
        env.holder.status = printing_status(filename)
        raise PrinterOffline("printer unreachable: ReadTimeout")

    monkeypatch.setattr(env.printer, "start_print", start_then_time_out)
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.STARTED
    assert env.kinds() == [JOB_STARTED]


async def test_head_map_error_fails_without_waiting_for_a_start(env):
    env.printer.fail_on["apply_head_map"] = PrinterError("head mapping did not take effect")
    env.bed.confirm()
    job = env.schedule()
    before = env.clock.now()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.FAILED, "head mapping did not take effect")
    assert ("start_print", "cube.gcode") not in env.printer.calls and env.clock.now() == before


async def test_printer_that_never_starts_fails(env):
    env.printer.on_start = None
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "printer did not start"


async def test_unreadable_file_skips_with_reason(env):  # Review Focus: printer errors
    env.printer.fail_on["file_info"] = PrinterError("GET /server/files/metadata failed: HTTP 500")
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == (
        "could not read file: GET /server/files/metadata failed: HTTP 500")


async def test_unexpected_error_fails_job_and_continues(env):
    def fail_first_read(filename: str) -> None:
        env.printer.on_file_info = None
        raise ValueError("boom")

    env.printer.on_file_info = fail_first_read
    env.bed.confirm()
    first, second = env.schedule(), env.schedule()
    await env.runner.tick()
    failed = env.jobs.get(first.id)
    assert (failed.status, failed.outcome_reason) == (JobStatus.FAILED, "internal error: ValueError")
    assert env.jobs.get(second.id).status is JobStatus.STARTED
    assert env.kinds() == [JOB_FAILED, JOB_STARTED]


async def test_cancel_while_reading_file_prevents_start(env):
    env.bed.confirm()
    job = env.schedule()
    env.printer.on_file_info = lambda filename: env.jobs.cancel(job.id)
    await env.runner.tick()
    assert [call[0] for call in env.printer.calls] == ["file_info"]
    assert env.jobs.get(job.id).status is JobStatus.CANCELLED
    assert env.kinds() == []
    assert isinstance(await env.bed.validity(), Valid)


async def test_edit_while_reading_file_is_not_acted_on(env):
    env.bed.confirm()
    job = env.schedule()
    tomorrow = job.scheduled_at + timedelta(days=1)
    env.printer.on_file_info = lambda filename: env.jobs.update(
        job.id, scheduled_at=tomorrow, head_map=job.head_map, file_size=job.file_size,
        file_modified=job.file_modified)
    await env.runner.tick()
    moved = env.jobs.get(job.id)
    assert (moved.status, moved.scheduled_at) == (JobStatus.PENDING, tomorrow)
    assert [call[0] for call in env.printer.calls] == ["file_info"]
    assert env.kinds() == []


async def test_edit_during_earlier_job_is_not_skipped_with_old_data(env):
    env.bed.confirm()
    first, second = env.schedule(), env.schedule()
    tomorrow = second.scheduled_at + timedelta(days=1)
    # The printer never reports printing, so checking the first start takes 5 s and the second
    # job's old time passes the 10 min grace meanwhile.
    env.printer.on_start = lambda filename: env.jobs.update(
        second.id, scheduled_at=tomorrow, head_map=second.head_map, file_size=second.file_size,
        file_modified=second.file_modified)
    env.clock.advance(timedelta(minutes=10) - timedelta(seconds=3))
    await env.runner.tick()
    moved = env.jobs.get(second.id)
    assert (moved.status, moved.scheduled_at) == (JobStatus.PENDING, tomorrow)
    assert env.kinds() == [JOB_FAILED]  # only the first job's "printer did not start"


async def test_edit_during_failed_file_read_is_not_skipped(env):
    env.bed.confirm()
    job = env.schedule()
    tomorrow = job.scheduled_at + timedelta(days=1)

    def edit_then_fail(filename: str) -> None:
        env.jobs.update(job.id, scheduled_at=tomorrow, head_map=job.head_map,
                        file_size=job.file_size, file_modified=job.file_modified)
        raise PrinterError("GET /server/files/metadata failed: HTTP 500")

    env.printer.on_file_info = edit_then_fail
    await env.runner.tick()
    moved = env.jobs.get(job.id)
    assert (moved.status, moved.scheduled_at) == (JobStatus.PENDING, tomorrow)
    assert env.kinds() == []


async def test_head_map_edit_during_bed_check_is_not_acted_on(env, monkeypatch):
    env.bed.confirm()
    job = env.schedule()
    new_map = (HeadAssignment(0, 1, "PLA", "000000"),)

    async def edit_then_valid(*, notify: bool = True):
        env.jobs.update(job.id, scheduled_at=job.scheduled_at, head_map=new_map,
                        file_size=job.file_size, file_modified=job.file_modified)
        return Valid(env.bed.active())

    monkeypatch.setattr(env.bed, "validity", edit_then_valid)
    await env.runner.tick()
    edited = env.jobs.get(job.id)
    assert (edited.status, edited.head_map) == (JobStatus.PENDING, new_map)
    assert [call[0] for call in env.printer.calls] == ["file_info"]
    assert env.kinds() == []


async def test_changed_file_skips(env):
    env.bed.confirm()
    job = env.schedule()
    env.printer.files_by_name["cube.gcode"] = make_file(size=5)
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "file changed since scheduling"


async def test_second_job_at_the_same_time_is_skipped(env):
    env.bed.confirm()
    first, second = env.schedule(), env.schedule()
    await env.runner.tick()
    assert env.jobs.get(first.id).status is JobStatus.STARTED
    assert env.jobs.get(second.id).outcome_reason == "printer busy"


async def test_startup_recovers_interrupted_and_missed_jobs(env):
    interrupted = env.schedule(-1)
    env.jobs.set_status(interrupted.id, JobStatus.STARTING)
    missed, recent = env.schedule(-20), env.schedule(-5)
    await env.runner.startup()
    assert env.jobs.get(interrupted.id).outcome_reason == "interrupted"
    assert env.jobs.get(missed.id).outcome_reason == "missed (app not running)"
    assert env.jobs.get(recent.id).status is JobStatus.PENDING


async def test_reminder_sent_once_when_unconfirmed(env):
    job = env.schedule(30)
    await env.runner.tick()
    await env.runner.tick()
    assert env.kinds() == [REMINDER]
    assert env.jobs.get(job.id).reminder_sent


async def test_reminder_skips_job_edited_during_bed_check(env, monkeypatch):
    job = env.schedule(30)

    async def edit_then_invalid(*, notify: bool = True):
        env.jobs.update(job.id, scheduled_at=job.scheduled_at + timedelta(minutes=15),
                        head_map=job.head_map, file_size=job.file_size,
                        file_modified=job.file_modified)
        return Invalid(NO_CONFIRMATION)

    monkeypatch.setattr(env.bed, "validity", edit_then_invalid)
    await env.runner.tick()
    assert env.kinds() == []
    assert not env.jobs.get(job.id).reminder_sent


async def test_failing_reminder_pass_still_runs_due_jobs(env, monkeypatch, caplog):
    async def disk_full(now):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(env.runner, "_remind", disk_full)
    env.bed.confirm()
    job = env.schedule()
    with caplog.at_level(logging.ERROR):
        await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.STARTED
    assert [r.getMessage() for r in caplog.records] == ["reminder pass failed"]


async def test_no_reminder_when_confirmed_or_far_away(env):
    env.bed.confirm()
    env.schedule(30)
    env.schedule(120)
    await env.runner.tick()
    assert env.kinds() == []


async def test_run_survives_startup_error_and_retries(env, monkeypatch):
    runner = env.runner_with(tick_seconds=0.02)
    calls = {"startup": 0, "tick": 0}
    real_startup, real_tick = runner.startup, runner.tick

    async def flaky_startup():
        calls["startup"] += 1
        if calls["startup"] == 1:
            raise RuntimeError("database is locked")
        await real_startup()

    async def counting_tick():
        calls["tick"] += 1
        await real_tick()

    monkeypatch.setattr(runner, "startup", flaky_startup)
    monkeypatch.setattr(runner, "tick", counting_tick)
    monkeypatch.setattr(runner, "_sleep_seconds", lambda: 0.02)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    task.cancel()
    assert calls["startup"] == 2 and calls["tick"] >= 1


async def test_run_survives_sleep_calculation_error(env, monkeypatch):
    runner = env.runner_with(tick_seconds=0.02)
    ticks = 0
    real_tick = runner.tick

    async def counting_tick():
        nonlocal ticks
        ticks += 1
        await real_tick()

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runner, "tick", counting_tick)
    monkeypatch.setattr(runner, "_sleep_seconds", broken)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    assert not task.done()
    task.cancel()
    assert ticks >= 2
