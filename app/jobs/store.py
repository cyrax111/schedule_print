"""SQLite storage for scheduled jobs."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime

from app.clock import Clock
from app.db import from_db, to_db

from .models import TERMINAL, HeadAssignment, Job, JobStatus


class JobNotEditable(Exception):
    """The job already ran, was skipped, failed or was cancelled."""


def _dump_map(head_map: Sequence[HeadAssignment]) -> str:
    return json.dumps([asdict(a) for a in head_map])


def _load_map(text: str) -> tuple[HeadAssignment, ...]:
    return tuple(HeadAssignment(**item) for item in json.loads(text))


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        filename=row["filename"],
        file_size=row["file_size"],
        file_modified=row["file_modified"],
        scheduled_at=from_db(row["scheduled_at_utc"]),
        head_map=_load_map(row["head_map_json"]),
        status=JobStatus(row["status"]),
        outcome_reason=row["outcome_reason"],
        reminder_sent=bool(row["reminder_sent"]),
        created_at=from_db(row["created_at"]),
        decided_at=from_db(row["decided_at"]),
    )


class JobStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def create(self, *, filename: str, file_size: int, file_modified: float,
               scheduled_at: datetime, head_map: Sequence[HeadAssignment]) -> Job:
        cursor = self._conn.execute(
            "INSERT INTO jobs (filename, file_size, file_modified, scheduled_at_utc, head_map_json,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (filename, file_size, file_modified, to_db(scheduled_at), _dump_map(head_map),
             to_db(self._clock.now())),
        )
        return self.get(cursor.lastrowid)

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job(row)

    def update(self, job_id: int, *, scheduled_at: datetime, head_map: Sequence[HeadAssignment],
               file_size: int, file_modified: float) -> Job:
        cursor = self._conn.execute(
            "UPDATE jobs SET scheduled_at_utc = ?, head_map_json = ?, file_size = ?,"
            " file_modified = ?, reminder_sent = 0 WHERE id = ? AND status = 'pending'",
            (to_db(scheduled_at), _dump_map(head_map), file_size, file_modified, job_id),
        )
        if cursor.rowcount == 0:
            raise JobNotEditable(job_id)
        return self.get(job_id)

    def cancel(self, job_id: int) -> None:
        cursor = self._conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ? WHERE id = ? AND status = 'pending'",
            (to_db(self._clock.now()), job_id),
        )
        if cursor.rowcount == 0:
            raise JobNotEditable(job_id)

    def claim(self, job_id: int, scheduled_at: datetime) -> bool:
        """Atomically move a pending job to 'starting', only if it is unchanged since it was read."""
        cursor = self._conn.execute(
            "UPDATE jobs SET status = 'starting' "
            "WHERE id = ? AND status = 'pending' AND scheduled_at_utc = ?",
            (job_id, to_db(scheduled_at)),
        )
        return cursor.rowcount == 1

    def set_status(self, job_id: int, status: JobStatus, reason: str | None = None) -> bool:
        """Change a job that is still pending or starting; finished jobs are left alone."""
        decided = to_db(self._clock.now()) if status in TERMINAL else None
        cursor = self._conn.execute(
            "UPDATE jobs SET status = ?, outcome_reason = ?, decided_at = ? "
            "WHERE id = ? AND status IN ('pending', 'starting')",
            (status.value, reason, decided, job_id),
        )
        return cursor.rowcount == 1

    def mark_reminder_sent(self, job_id: int) -> None:
        self._conn.execute("UPDATE jobs SET reminder_sent = 1 WHERE id = ?", (job_id,))

    def _select(self, where: str, params: tuple = (), order: str = "scheduled_at_utc, id",
                limit: int | None = None) -> list[Job]:
        sql = f"SELECT * FROM jobs WHERE {where} ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_job(row) for row in self._conn.execute(sql, params).fetchall()]

    def pending(self) -> list[Job]:
        return self._select("status = 'pending'")

    def due(self, now: datetime) -> list[Job]:
        return self._select("status = 'pending' AND scheduled_at_utc <= ?", (to_db(now),))

    def in_status(self, status: JobStatus) -> list[Job]:
        return self._select("status = ?", (status.value,))

    def finished(self, limit: int = 50) -> list[Job]:
        return self._select("status != 'pending'",
                            order="COALESCE(decided_at, scheduled_at_utc) DESC, id DESC", limit=limit)

    def count_pending(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()[0]

    def previous_pending(self, before: datetime, exclude_id: int | None = None) -> Job | None:
        jobs = self._select("status = 'pending' AND scheduled_at_utc < ? AND id != ?",
                            (to_db(before), exclude_id if exclude_id is not None else -1),
                            order="scheduled_at_utc DESC, id DESC", limit=1)
        return jobs[0] if jobs else None
