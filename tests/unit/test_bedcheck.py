import asyncio
import contextlib
import sqlite3
from datetime import timedelta

import pytest

from app.bedcheck.service import (
    NO_CONFIRMATION, PRINTED, UNVERIFIED, WITHDRAWN, BedCheck, ConfirmRefused, Invalid, Valid,
)
from app.db import from_db
from app.printer.events import Connected, Disconnected, Heartbeat, PrintFinished, PrintStarted
from app.printer.models import HistoryJob, PrinterStatus
from tests.factories import make_status, printing_status
from tests.fakes import FakeHistory, StatusHolder, eventually


@pytest.fixture
def holder():
    return StatusHolder(make_status())


@pytest.fixture
def history():
    return FakeHistory()


@pytest.fixture
def voids():
    return []


@pytest.fixture
def bed(db, clock, history, holder, voids):
    async def on_voided(reason):
        voids.append(reason)

    check = BedCheck(db, clock, history, lambda: holder.status, on_voided=on_voided)
    check.startup()
    return check


async def connected(bed, clock, status=None):
    await bed.handle_event(Connected(at=clock.now(), status=status or make_status()))


class FailingWrites:
    """Forwards to a real connection, but writes to the named tables raise like a full disk."""

    def __init__(self, conn, tables) -> None:
        self._conn = conn
        self._tables = tables

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE")) and any(t in sql for t in self._tables):
            raise sqlite3.OperationalError("disk full")
        return self._conn.execute(sql, params)


class GatedHistory:
    """History fake that can hold its answer until the test releases it."""

    def __init__(self) -> None:
        self.hold = False
        self.release = asyncio.Event()
        self.calls: list = []

    async def history(self, since):
        self.calls.append(since)
        if self.hold:
            await self.release.wait()
        return []


async def gap(bed, clock, seconds):
    """Lose the printer for `seconds`, then reconnect."""
    await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(seconds)
    await connected(bed, clock)


async def test_confirm_needs_watched_idle_printer(bed, clock, holder):
    with pytest.raises(ConfirmRefused, match="not watching"):
        bed.confirm()
    await connected(bed, clock)
    holder.status = printing_status()
    with pytest.raises(ConfirmRefused, match="printer is printing"):
        bed.confirm()
    holder.status = PrinterStatus.offline()
    with pytest.raises(ConfirmRefused, match="printer is offline"):
        bed.confirm()


async def test_confirmation_stays_valid_while_watched(bed, clock, history):
    await connected(bed, clock)
    confirmation = bed.confirm()
    for _ in range(5 * 120):  # five hours of heartbeats every 30 s
        clock.advance(30)
        await bed.handle_event(Heartbeat(at=clock.now()))
    assert await bed.validity() == Valid(confirmation)
    assert history.calls == []


async def suspended(bed, clock, hours=3):
    """Confirm while watching, then the host sleeps: no events at all for `hours`."""
    await connected(bed, clock)
    bed.confirm()
    clock.advance(30)
    await bed.handle_event(Heartbeat(at=clock.now()))
    last_seen = clock.now()
    clock.advance(timedelta(hours=hours))
    return last_seen


async def test_heartbeat_after_a_suspend_does_not_cover_it(bed, clock, history, voids):
    history.jobs = None
    await suspended(bed, clock)
    await bed.handle_event(Heartbeat(at=clock.now()))
    assert await bed.validity() == Invalid(UNVERIFIED)
    assert bed.active() is None and voids == [UNVERIFIED]


async def test_suspend_is_settled_through_history(bed, clock, history):
    last_seen = await suspended(bed, clock)
    await bed.handle_event(Heartbeat(at=clock.now()))
    assert isinstance(await bed.validity(), Valid)
    assert history.calls == [last_seen - timedelta(minutes=2)]


async def test_disconnect_after_a_suspend_ends_the_period_at_its_last_heartbeat(bed, clock, db):
    last_seen = await suspended(bed, clock)
    await bed.handle_event(Disconnected(at=clock.now()))
    row = db.execute("SELECT last_seen_at, closed FROM watch_periods").fetchone()
    assert (from_db(row["last_seen_at"]), row["closed"]) == (last_seen, 1)
    assert bed.watching is False


async def test_suspend_is_not_watched_time_before_the_next_event(bed, clock, history):
    history.jobs = None
    await suspended(bed, clock)
    assert await bed.validity() == Invalid(UNVERIFIED)  # e.g. the runner ticks before the watcher


async def test_new_confirmation_replaces_old_quietly(bed, clock, db, voids):
    await connected(bed, clock)
    first = bed.confirm()
    second = bed.confirm()
    assert bed.active().id == second.id
    reason = db.execute("SELECT end_reason FROM confirmations WHERE id = ?", (first.id,)).fetchone()[0]
    assert reason == "replaced" and voids == []


async def test_print_start_voids_and_notifies(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(PrintStarted(at=clock.now(), filename="x.gcode"))
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]


async def test_print_finish_voids_and_notifies(bed, clock, voids, db):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(PrintFinished(at=clock.now(), filename="x.gcode", result="complete"))
    assert bed.active() is None
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]
    assert db.execute("SELECT kind FROM print_events").fetchone()[0] == "finished"


async def test_withdraw_is_quiet(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    bed.withdraw()
    assert await bed.validity() == Invalid(WITHDRAWN) and voids == []


async def test_consumed_confirmation_reads_as_none(bed, clock):
    await connected(bed, clock)
    bed.confirm()
    bed.consume(job_id=7)
    assert await bed.validity() == Invalid(NO_CONFIRMATION)
    assert bed.latest().consumed_by_job_id == 7


async def test_short_disconnect_needs_no_history(bed, clock, history):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 60)
    assert isinstance(await bed.validity(), Valid) and history.calls == []


async def test_long_gap_is_checked_against_history(bed, clock, history):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    gap_start = clock.now()
    await gap(bed, clock, 600)
    assert isinstance(await bed.validity(), Valid)
    assert history.calls[0] == gap_start - timedelta(minutes=2)


async def test_print_found_in_history_voids_on_reconnect(bed, clock, history, voids, db):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    history.jobs = [HistoryJob("sneaky.gcode", clock.now() + timedelta(minutes=3), "completed")]
    await gap(bed, clock, 600)
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]
    assert db.execute("SELECT source FROM print_events").fetchone()[0] == "history"


async def test_history_unavailable_voids_as_unverified(bed, clock, history):
    history.jobs = None
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 600)
    assert await bed.validity() == Invalid(UNVERIFIED)


async def test_reconnect_while_printing_voids(bed, clock):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(Disconnected(at=clock.now()))
    await connected(bed, clock, status=printing_status())
    assert await bed.validity() == Invalid(PRINTED)


async def test_while_disconnected_it_is_unverified_but_not_voided(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(600)
    assert await bed.validity() == Invalid(UNVERIFIED)
    assert bed.active() is not None and voids == []


async def test_app_restart_gap_is_checked(db, clock, history, holder):
    first = BedCheck(db, clock, history, lambda: holder.status)
    first.startup()
    await connected(first, clock)
    first.confirm()
    clock.advance(30)
    await first.handle_event(Heartbeat(at=clock.now()))
    clock.advance(600)  # process died without a Disconnected event
    history.jobs = None
    second = BedCheck(db, clock, history, lambda: holder.status)
    second.startup()
    await connected(second, clock)
    assert await second.validity() == Invalid(UNVERIFIED)


async def test_validity_without_notify_is_silent(bed, clock, history, voids):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 600)  # history [] → still valid
    history.jobs = None
    assert await bed.validity(notify=False) == Invalid(UNVERIFIED)
    assert voids == []


async def test_watching_follows_the_watch_period(bed, clock):
    assert bed.watching is False
    await connected(bed, clock)
    assert bed.watching is True
    await bed.handle_event(Disconnected(at=clock.now()))
    assert bed.watching is False


async def test_failed_period_write_does_not_leave_it_watching(bed, clock, db, history):
    await connected(bed, clock)
    bed.confirm()
    bed._conn = FailingWrites(db, ("watch_periods",))
    with contextlib.suppress(sqlite3.OperationalError):
        await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(3600)
    history.jobs = None
    with contextlib.suppress(sqlite3.OperationalError):
        await bed.handle_event(Connected(at=clock.now(), status=make_status()))
    assert bed.watching is False
    assert not isinstance(await bed.validity(), Valid)


async def test_print_started_voids_even_if_bookkeeping_fails(bed, clock, db):
    await connected(bed, clock)
    bed.confirm()
    bed._conn = FailingWrites(db, ("print_events", "watch_periods"))
    with contextlib.suppress(sqlite3.OperationalError):
        await bed.handle_event(PrintStarted(at=clock.now(), filename="x.gcode"))
    assert bed.active() is None


async def test_void_during_history_wait_is_not_valid(db, clock, holder):
    history = GatedHistory()
    check = BedCheck(db, clock, history, lambda: holder.status)
    check.startup()
    await connected(check, clock)
    check.confirm()
    clock.advance(60)
    await gap(check, clock, 600)  # history answers at once here
    history.hold = True
    pending = asyncio.create_task(check.validity())
    await eventually(lambda: len(history.calls) == 2)
    await check.handle_event(PrintStarted(at=clock.now(), filename="x.gcode"))
    history.release.set()
    assert await pending == Invalid(PRINTED)


async def test_job_just_after_gap_end_still_voids(bed, clock, history, voids):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(600)
    history.jobs = [HistoryJob("late.gcode", clock.now() + timedelta(seconds=30), "completed")]
    await connected(bed, clock)  # printer clock ahead of ours: the job starts after the gap
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]


async def test_history_is_asked_from_before_the_gap(bed, clock, history):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    gap_start = clock.now()
    history.jobs = [HistoryJob("early.gcode", gap_start - timedelta(seconds=60), "completed")]
    await gap(bed, clock, 600)
    assert history.calls[0] == gap_start - timedelta(minutes=2)
    assert await bed.validity() == Invalid(PRINTED)
