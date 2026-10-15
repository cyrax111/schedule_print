"""Notification feed (SQLite) and Telegram delivery state."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock
from app.db import from_db, to_db

log = logging.getLogger(__name__)

JOB_STARTED = "job_started"
JOB_SKIPPED = "job_skipped"
JOB_FAILED = "job_failed"
PRINT_FINISHED = "print_finished"
REMINDER = "reminder"
CONFIRMATION_VOIDED = "confirmation_voided"
TEST = "test"


@dataclass(frozen=True)
class Notification:
    id: int
    at: datetime
    kind: str
    text: str
    job_id: int | None
    read: bool
    telegram_status: str
    telegram_attempts: int
    telegram_error: str | None
    telegram_next_at: datetime | None


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"], at=from_db(row["at"]), kind=row["kind"], text=row["text"],
        job_id=row["job_id"], read=bool(row["read"]), telegram_status=row["telegram_status"],
        telegram_attempts=row["telegram_attempts"], telegram_error=row["telegram_error"],
        telegram_next_at=from_db(row["telegram_next_at"]),
    )


class Notifier:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, *, telegram_enabled: bool) -> None:
        self._conn = conn
        self._clock = clock
        self._telegram_enabled = telegram_enabled
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def notify(self, kind: str, text: str, job_id: int | None = None) -> None:
        """Record a notification. Never raises: scheduling must not depend on it."""
        try:
            now = to_db(self._clock.now())
            queued = self._telegram_enabled
            self._conn.execute(
                "INSERT INTO notifications (at, kind, text, job_id, telegram_status, telegram_next_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, kind, text, job_id, "pending" if queued else "off", now if queued else None),
            )
        except Exception:
            log.exception("could not record %s notification", kind)
            return
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                log.exception("notification listener failed")

    def latest(self, limit: int = 5) -> list[Notification]:
        rows = self._conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
        return [_notification(row) for row in rows.fetchall()]

    def unread_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()[0]

    def mark_all_read(self) -> None:
        self._conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")

    def due_for_telegram(self, now: datetime) -> list[Notification]:
        rows = self._conn.execute(
            "SELECT * FROM notifications WHERE telegram_status = 'pending' AND telegram_next_at <= ?"
            " ORDER BY id", (to_db(now),))
        return [_notification(row) for row in rows.fetchall()]

    def telegram_sent(self, notification_id: int) -> None:
        self._conn.execute(
            "UPDATE notifications SET telegram_status = 'sent', telegram_attempts = telegram_attempts + 1,"
            " telegram_error = NULL, telegram_next_at = NULL WHERE id = ?", (notification_id,))

    def telegram_failed(self, notification_id: int, error: str, next_at: datetime | None) -> None:
        self._conn.execute(
            "UPDATE notifications SET telegram_status = ?, telegram_attempts = telegram_attempts + 1,"
            " telegram_error = ?, telegram_next_at = ? WHERE id = ?",
            ("failed" if next_at is None else "pending", error,
             None if next_at is None else to_db(next_at), notification_id),
        )
