"""Start-or-skip decision for a due job (spec §8). Pure: no I/O."""
from __future__ import annotations

from dataclasses import dataclass

from app.bedcheck.service import Invalid, Validity
from app.printer.models import FileInfo, HeadFilament, PrinterStatus, PrintState

from .heads import describe_filament
from .models import Job


@dataclass(frozen=True)
class GateSnapshot:
    job: Job
    status: PrinterStatus
    validity: Validity
    file: FileInfo | None


@dataclass(frozen=True)
class Start:
    pass


@dataclass(frozen=True)
class Skip:
    reason: str


Decision = Start | Skip


def _matches(head: HeadFilament | None, expected_type: str | None, expected_rgb: str | None) -> bool:
    """Unknown never matches: both the loaded filament and the expectation must be identified."""
    if head is None or not head.present or head.material is None or head.rgb is None:
        return False
    if expected_type is None or expected_rgb is None:
        return False
    return head.material.strip().lower() == expected_type.strip().lower() and head.rgb == expected_rgb


def gate(snapshot: GateSnapshot) -> Decision:
    status, job, file = snapshot.status, snapshot.job, snapshot.file
    if not status.online:
        return Skip("printer offline")
    if status.exception:
        return Skip(f"printer in error: {status.exception}")
    if not status.klippy_ready:
        return Skip("printer not ready")
    if not status.idle:
        if status.state is PrintState.ERROR:
            return Skip("printer in error: last print ended with an error")
        return Skip("printer busy")
    if isinstance(snapshot.validity, Invalid):
        return Skip(snapshot.validity.reason)
    if file is None:
        return Skip("file missing")
    if (file.size, file.modified) != (job.file_size, job.file_modified):
        return Skip("file changed since scheduling")
    if not job.head_map:
        return Skip("no head assignments (the file has no colour information)")
    heads = {h.head: h for h in status.heads}
    for assignment in job.head_map:
        head = heads.get(assignment.head)
        if not _matches(head, assignment.expected_type, assignment.expected_rgb):
            loaded = head is not None and head.present
            found = describe_filament(head.rgb, head.material) if loaded else "nothing"
            expected = describe_filament(assignment.expected_rgb, assignment.expected_type)
            return Skip(f"head {assignment.head + 1}: expected {expected}, found {found}")
    return Start()
