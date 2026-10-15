"""Runs due jobs: offline retries, the gate, the start sequence, reminders and recovery."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.bedcheck.service import BedCheck, Valid
from app.clock import Clock
from app.notify import messages
from app.notify.service import JOB_FAILED, JOB_SKIPPED, JOB_STARTED, REMINDER, Notifier
from app.printer.client import PrinterError, PrinterOffline
from app.printer.models import FileInfo, PrinterStatus, PrintState

from .gate import GateSnapshot, Skip, gate
from .models import Job, JobStatus
from .store import JobStore

log = logging.getLogger(__name__)


def _error_text(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


@dataclass(frozen=True)
class RunnerSettings:
    tz: ZoneInfo
    dry_run: bool = False
    offline_grace: timedelta = timedelta(minutes=10)
    start_verify_seconds: int = 90
    reminder: timedelta = timedelta(minutes=60)
    tick_seconds: float = 30.0


class PrinterPort(Protocol):
    async def file_info(self, filename: str) -> FileInfo | None: ...

    async def apply_head_map(self, assignments: Sequence[tuple[int, int]]) -> None: ...

    async def start_print(self, filename: str) -> None: ...


class Runner:
    def __init__(self, *, jobs: JobStore, bedcheck: BedCheck, printer: PrinterPort,
                 status: Callable[[], PrinterStatus], notifier: Notifier, clock: Clock,
                 settings: RunnerSettings) -> None:
        self._jobs = jobs
        self._bedcheck = bedcheck
        self._printer = printer
        self._status = status
        self._notifier = notifier
        self._clock = clock
        self._settings = settings
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        started = False
        while True:
            self._wake.clear()
            try:
                if not started:  # no tick before startup has cleaned up after a crash
                    await self.startup()
                    started = True
                await self.tick()
            except Exception:
                log.exception("scheduler %s failed", "tick" if started else "startup")
            try:
                timeout = self._sleep_seconds()
            except Exception:
                log.exception("scheduler could not work out its next wake-up")
                timeout = self._settings.tick_seconds
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def startup(self) -> None:
        for job in self._jobs.in_status(JobStatus.STARTING):
            self._finish(job, JobStatus.FAILED, "interrupted")
        now = self._clock.now()
        for job in self._jobs.pending():
            if now - job.scheduled_at > self._settings.offline_grace:
                self._finish(job, JobStatus.SKIPPED, "missed (app not running)")

    async def tick(self) -> None:
        now = self._clock.now()
        try:
            await self._remind(now)
        except Exception:  # a reminder problem must not hold up due jobs
            log.exception("reminder pass failed")
        for job in self._jobs.due(now):
            try:
                await self._run_job(job)
            except Exception as exc:
                log.exception("job %s failed unexpectedly", job.id)
                self._finish(job, JobStatus.FAILED, f"internal error: {exc.__class__.__name__}")

    def _sleep_seconds(self) -> float:
        now = self._clock.now()
        moments: list[datetime] = []
        for job in self._jobs.pending():
            moments.append(job.scheduled_at)
            if not job.reminder_sent:
                moments.append(job.scheduled_at - self._settings.reminder)
        upcoming = [(m - now).total_seconds() for m in moments if m > now]
        return max(0.5, min([self._settings.tick_seconds, *upcoming]))

    async def _remind(self, now: datetime) -> None:
        for job in self._jobs.pending():
            in_window = job.scheduled_at - self._settings.reminder <= now < job.scheduled_at
            if job.reminder_sent or not in_window:
                continue
            validity = await self._bedcheck.validity(notify=False)
            if not self._unchanged(job) or isinstance(validity, Valid):
                continue  # an edited job is reconsidered, with its new time, on a later tick
            self._notifier.notify(REMINDER, messages.reminder(job, validity.reason, self._settings.tz),
                                  job.id)
            self._jobs.mark_reminder_sent(job.id)

    def _unchanged(self, job: Job) -> bool:
        """False if the job was deleted, cancelled or edited since `job` was read."""
        return self._jobs.get(job.id) == job

    def _late(self, job: Job) -> bool:
        """Never start late: more than the offline grace after the job's time."""
        return self._clock.now() - job.scheduled_at > self._settings.offline_grace

    def _missed(self) -> str:
        return f"missed (more than {self._settings.offline_grace // timedelta(minutes=1)} min late)"

    def _skipped_as_late(self, job: Job) -> bool:
        """Skip `job` if it is late; True when it was."""
        if not self._late(job):
            return False
        reason = self._missed() if self._status().online else "printer offline"
        self._finish(job, JobStatus.SKIPPED, reason)
        return True

    async def _run_job(self, job: Job) -> None:
        if not self._unchanged(job):
            return  # changed while an earlier job ran; a later tick sees the fresh data
        if self._skipped_as_late(job):
            return
        if not self._status().online:
            return  # retry on a later tick
        error: PrinterError | None = None
        try:
            file = await self._printer.file_info(job.filename)
        except PrinterError as exc:
            file, error = None, exc
        if not self._unchanged(job):
            return  # changed while we read the file; a later tick sees the fresh data
        if isinstance(error, PrinterOffline):
            return  # retry on a later tick
        if error is not None:
            self._finish(job, JobStatus.SKIPPED, f"could not read file: {error}")
            return
        validity = await self._bedcheck.validity(notify=False)
        if not self._unchanged(job):
            return  # changed while we checked the bed; a later tick sees the fresh data
        if self._skipped_as_late(job):  # the reads above may have taken us past the grace
            return  # no await from here to claim, so this holds at claim time
        decision = gate(GateSnapshot(job=job, status=self._status(), validity=validity, file=file))
        if isinstance(decision, Skip):
            self._finish(job, JobStatus.SKIPPED, decision.reason)
            return
        if self._settings.dry_run:
            log.info("DRY RUN: would map heads %s and start %s",
                     [(a.logical, a.head) for a in job.head_map], job.filename)
            self._finish(job, JobStatus.SKIPPED, "dry run (all checks passed)")
            return
        if not self._jobs.claim(job.id, job.scheduled_at):
            return
        start_error: PrinterError | None = None
        try:
            self._bedcheck.consume(job.id)
            await self._printer.apply_head_map([(a.logical, a.head) for a in job.head_map])
            if self._late(job):  # mapping took us past the grace; the confirmation stays used
                self._finish(job, JobStatus.FAILED, self._missed())
                return
            try:
                await self._printer.start_print(job.filename)
            except PrinterError as exc:  # e.g. a read timeout: the printer may have started anyway
                start_error = exc
        except Exception as exc:
            self._finish(job, JobStatus.FAILED, _error_text(exc))
            return
        if await self._verify_started(job.filename):
            self._jobs.set_status(job.id, JobStatus.STARTED)
            self._notifier.notify(JOB_STARTED, messages.job_started(job), job.id)
        else:
            reason = "printer did not start" if start_error is None else _error_text(start_error)
            self._finish(job, JobStatus.FAILED, reason)

    async def _verify_started(self, filename: str) -> bool:
        waited = 0.0
        while True:
            status = self._status()
            if status.state in (PrintState.PRINTING, PrintState.PAUSED) and status.filename == filename:
                return True
            if waited >= self._settings.start_verify_seconds:
                return False
            await self._clock.sleep(1.0)
            waited += 1.0

    def _finish(self, job: Job, status: JobStatus, reason: str) -> None:
        if not self._jobs.set_status(job.id, status, reason):
            return  # already finished or cancelled: leave it and stay quiet
        if status is JobStatus.SKIPPED:
            text = messages.job_skipped(job, reason, self._settings.tz)
            self._notifier.notify(JOB_SKIPPED, text, job.id)
        else:
            self._notifier.notify(JOB_FAILED, messages.job_failed(job, reason), job.id)
