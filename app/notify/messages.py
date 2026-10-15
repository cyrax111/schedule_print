"""Notification texts (pure)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app.jobs.heads import describe_filament
from app.jobs.models import Job
from app.timeutil import format_local


def _heads(job: Job) -> str:
    return ", ".join(f"head {a.head + 1} {describe_filament(a.expected_rgb, a.expected_type)}"
                     for a in job.head_map)


def job_started(job: Job) -> str:
    return f"Started {job.filename} ({_heads(job)})"


def job_skipped(job: Job, reason: str, tz: ZoneInfo) -> str:
    return f"Skipped {job.filename} at {format_local(job.scheduled_at, tz)}: {reason}"


def job_failed(job: Job, reason: str) -> str:
    return f"Failed {job.filename}: {reason}"


def print_finished(filename: str | None, result: str) -> str:
    return f"{filename or 'Print'} finished: {result}"


def reminder(job: Job, reason: str, tz: ZoneInfo) -> str:
    return (f"{job.filename} is due at {format_local(job.scheduled_at, tz)} "
            f"and the bed is not confirmed ({reason})")


def confirmation_voided(reason: str, pending: int) -> str:
    return f"Bed confirmation voided: {reason}. {pending} job{'' if pending == 1 else 's'} waiting."
