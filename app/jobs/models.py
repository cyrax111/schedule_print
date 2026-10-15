"""Scheduled print jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    STARTED = "started"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.STARTED, JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True)
class HeadAssignment:
    logical: int                  # colour number in the file
    head: int                     # 0-3
    expected_type: str | None     # material loaded in that head when scheduled
    expected_rgb: str | None      # colour loaded in that head when scheduled


@dataclass(frozen=True)
class Job:
    id: int
    filename: str
    file_size: int
    file_modified: float
    scheduled_at: datetime
    head_map: tuple[HeadAssignment, ...]
    status: JobStatus
    outcome_reason: str | None
    reminder_sent: bool
    created_at: datetime
    decided_at: datetime | None
