"""The bed-confirmation rule (spec §6): one confirmation, voided by any print."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.clock import Clock
from app.db import from_db, to_db
from app.printer.events import (
    Connected, Disconnected, Event, Heartbeat, PrintFinished, PrintStarted,
)
from app.printer.models import HistoryJob, PrinterStatus, PrintState

from .coverage import Period, uncovered_gaps

log = logging.getLogger(__name__)

GAP_TOLERANCE = timedelta(minutes=2)
# Longest silence that still counts as watched: the watcher's 30 s heartbeat plus keepalive slack.
# Longer means nothing ran (host suspend, stalled loop), so that stretch is a gap, not watched time.
MAX_SILENCE = timedelta(seconds=90)
# The printer's clock and the app's clock may differ, so history is searched from this long before a gap.
CLOCK_MARGIN = timedelta(minutes=2)
NO_CONFIRMATION = "no confirmation"
PRINTED = "printed since confirmation"
UNVERIFIED = "couldn't verify the printer stayed idle"
WITHDRAWN = "withdrawn"
REPLACED = "replaced"
CONSUMED = "consumed"


@dataclass(frozen=True)
class Confirmation:
    id: int
    confirmed_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    consumed_by_job_id: int | None


@dataclass(frozen=True)
class Valid:
    confirmation: Confirmation


@dataclass(frozen=True)
class Invalid:
    reason: str


Validity = Valid | Invalid


class ConfirmRefused(Exception):
    pass


class HistorySource(Protocol):
    async def history(self, since: datetime) -> list[HistoryJob] | None: ...


VoidListener = Callable[[str], Awaitable[None]]


def not_idle_reason(status: PrinterStatus) -> str:
    if not status.online:
        return "printer is offline"
    if status.exception:
        return f"printer reports an error: {status.exception}"
    if status.paused or status.state is PrintState.PAUSED:
        return "printer is paused"
    if status.state is PrintState.PRINTING:
        return "printer is printing"
    if not status.klippy_ready:
        return "printer is not ready"
    return f"printer is not idle ({status.state.value})"


def _confirmation(row: sqlite3.Row) -> Confirmation:
    return Confirmation(
        id=row["id"],
        confirmed_at=from_db(row["confirmed_at"]),
        ended_at=from_db(row["ended_at"]),
        end_reason=row["end_reason"],
        consumed_by_job_id=row["consumed_by_job_id"],
    )


class BedCheck:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, history: HistorySource,
                 status: Callable[[], PrinterStatus], on_voided: VoidListener | None = None) -> None:
        self._conn = conn
        self._clock = clock
        self._history = history
        self._status = status
        self._on_voided = on_voided
        self._open_period: int | None = None

    def set_void_listener(self, listener: VoidListener) -> None:
        self._on_voided = listener

    @property
    def watching(self) -> bool:
        """True while a watch period is open (read-only; touches neither history nor the database)."""
        return self._open_period is not None

    # -- lifecycle -------------------------------------------------------------------

    def startup(self) -> None:
        """Close watch periods left open by a crash; they end at their last heartbeat."""
        self._conn.execute("UPDATE watch_periods SET closed = 1 WHERE closed = 0")

    def shutdown(self) -> None:
        if self._open_period is not None:
            self._close_period(self._clock.now())

    async def handle_event(self, event: Event) -> None:
        if isinstance(event, Connected):
            self._open_period = None
            self._open_period = self._new_period(event.at)
            if event.status.paused or event.status.state in (PrintState.PRINTING, PrintState.PAUSED):
                await self._void_active(PRINTED, notify=True)
            else:
                await self.validity(notify=True)  # settles any gap since the last period now
        elif isinstance(event, Heartbeat):
            self._touch(event.at)
        elif isinstance(event, PrintStarted):
            await self._void_active(PRINTED, notify=True)
            self._touch(event.at)
            self._record_print("started", event.at, event.filename, None, "live")
        elif isinstance(event, PrintFinished):
            # A print ran while the bed was confirmed, even if its start was never seen.
            await self._void_active(PRINTED, notify=True)
            self._touch(event.at)
            self._record_print("finished", event.at, event.filename, event.result, "live")
        elif isinstance(event, Disconnected) and self._open_period is not None:
            self._close_period(event.at)

    # -- confirmations ---------------------------------------------------------------

    def latest(self) -> Confirmation | None:
        row = self._conn.execute("SELECT * FROM confirmations ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _confirmation(row)

    def active(self) -> Confirmation | None:
        latest = self.latest()
        return latest if latest is not None and latest.ended_at is None else None

    def confirm(self) -> Confirmation:
        status = self._status()
        if not status.online or not status.idle:
            raise ConfirmRefused(not_idle_reason(status))
        if self._open_period is None:
            raise ConfirmRefused("not watching the printer yet; try again in a moment")
        now = self._clock.now()
        self._end_active(now, REPLACED)
        self._conn.execute("INSERT INTO confirmations (confirmed_at) VALUES (?)", (to_db(now),))
        return self.latest()

    def withdraw(self) -> None:
        self._end_active(self._clock.now(), WITHDRAWN)

    def consume(self, job_id: int) -> None:
        current = self.active()
        if current is None:
            raise ConfirmRefused("no active confirmation to use")
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ?, consumed_by_job_id = ? "
            "WHERE id = ?",
            (to_db(self._clock.now()), CONSUMED, job_id, current.id),
        )

    async def validity(self, *, notify: bool = True) -> Validity:
        current = self.active()
        if current is None:
            latest = self.latest()
            if latest is None or latest.end_reason in (CONSUMED, REPLACED, None):
                return Invalid(NO_CONFIRMATION)
            return Invalid(latest.end_reason)
        if self._open_period is None:
            return Invalid(UNVERIFIED)  # not watching right now; settled again on reconnect
        now = self._clock.now()
        gaps = uncovered_gaps(self._periods(current.confirmed_at, now), current.confirmed_at,
                              now, GAP_TOLERANCE)
        if not gaps:
            return Valid(current)
        since = gaps[0].start - CLOCK_MARGIN
        jobs = await self._history.history(since=since)
        active = self.active()
        if active is None or active.id != current.id or self._open_period is None:
            # Changed while we waited for history: answer from the stored state.
            return await self.validity(notify=notify)
        if jobs is None:
            reason = UNVERIFIED
        else:
            # Any print started since `since` is unexplained. No per-gap window: a window would let
            # clock skew hide a print. This is safe because while a confirmation is active, live
            # starts have already voided it, and the app's own jobs consume it first.
            inside = [j for j in jobs if j.start_time >= since]
            if not inside:
                return Valid(current)
            for job in inside:
                self._record_print("started", job.start_time, job.filename, job.status, "history")
            reason = PRINTED
        await self._void(current.id, reason, notify)
        return Invalid(reason)

    # -- helpers ---------------------------------------------------------------------

    def _periods(self, since: datetime, now: datetime) -> list[Period]:
        rows = self._conn.execute(
            "SELECT id, began_at, last_seen_at FROM watch_periods WHERE last_seen_at >= ? OR id = ?",
            (to_db(since), self._open_period),
        ).fetchall()
        periods = []
        for r in rows:
            end = from_db(r["last_seen_at"])
            if r["id"] == self._open_period and now - end <= MAX_SILENCE:
                end = now  # watching right now; a silent open period ends at its last sign of life
            periods.append(Period(from_db(r["began_at"]), end))
        return periods

    def _end_active(self, now: datetime, reason: str) -> None:
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ? WHERE ended_at IS NULL",
            (to_db(now), reason),
        )

    async def _void(self, confirmation_id: int, reason: str, notify: bool) -> None:
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
            (to_db(self._clock.now()), reason, confirmation_id),
        )
        if notify and self._on_voided is not None:
            try:
                await self._on_voided(reason)
            except Exception:
                log.exception("void listener failed")

    async def _void_active(self, reason: str, notify: bool) -> None:
        current = self.active()
        if current is not None:
            await self._void(current.id, reason, notify)

    def _new_period(self, at: datetime) -> int:
        return self._conn.execute(
            "INSERT INTO watch_periods (began_at, last_seen_at) VALUES (?, ?)",
            (to_db(at), to_db(at)),
        ).lastrowid

    def _last_seen(self, period: int) -> datetime:
        row = self._conn.execute("SELECT last_seen_at FROM watch_periods WHERE id = ?",
                                 (period,)).fetchone()
        return from_db(row["last_seen_at"])

    def _touch(self, at: datetime) -> None:
        if self._open_period is None:
            return
        if at - self._last_seen(self._open_period) > MAX_SILENCE:
            # Nothing ran in between: end the period at its last sign of life and open a new one,
            # so validity() sees the gap and settles it through history.
            self._close_period(at)
            self._open_period = self._new_period(at)
        else:
            self._conn.execute("UPDATE watch_periods SET last_seen_at = ? WHERE id = ?",
                               (to_db(at), self._open_period))

    def _close_period(self, at: datetime) -> None:
        """Close the open period at `at`, or at its last sign of life if it was silent too long."""
        period, self._open_period = self._open_period, None
        last_seen = self._last_seen(period)
        end = last_seen if at - last_seen > MAX_SILENCE else at
        self._conn.execute("UPDATE watch_periods SET last_seen_at = ?, closed = 1 WHERE id = ?",
                           (to_db(end), period))

    def _record_print(self, kind: str, at: datetime, filename: str | None, result: str | None,
                      source: str) -> None:
        self._conn.execute(
            "INSERT INTO print_events (at, kind, filename, result, source) VALUES (?, ?, ?, ?, ?)",
            (to_db(at), kind, filename, result, source),
        )
