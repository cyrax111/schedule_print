from datetime import datetime, timezone

import pytest

from app.bedcheck.service import Confirmation, Invalid, Valid
from app.jobs.gate import GateSnapshot, Skip, Start, gate
from app.jobs.models import HeadAssignment
from app.printer.models import HeadFilament, PrinterStatus, PrintState
from tests.factories import DEFAULT_HEADS, make_file, make_job, make_status, printing_status

T = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
VALID = Valid(Confirmation(1, T, None, None, None))
JOB = make_job()  # expects Head 3 = #FF0000 PETG, file size 1000, modified 1_760_000_000.0


def snap(**overrides) -> GateSnapshot:
    values = dict(job=JOB, status=make_status(), validity=VALID, file=make_file())
    values.update(overrides)
    return GateSnapshot(**values)


def head3(**fields) -> PrinterStatus:
    return make_status(heads=(*DEFAULT_HEADS[:2], HeadFilament(2, **fields), DEFAULT_HEADS[3]))


@pytest.mark.parametrize("snapshot, reason", [
    (snap(status=PrinterStatus.offline()), "printer offline"),
    (snap(status=make_status(idle=False, exception="Nozzle clog")), "printer in error: Nozzle clog"),
    (snap(status=make_status(idle=False, klippy_ready=False)), "printer not ready"),
    (snap(status=make_status(idle=False, state=PrintState.ERROR)),
     "printer in error: last print ended with an error"),
    (snap(status=printing_status()), "printer busy"),
    (snap(validity=Invalid("no confirmation")), "no confirmation"),
    (snap(validity=Invalid("printed since confirmation")), "printed since confirmation"),
    (snap(file=None), "file missing"),
    (snap(file=make_file(size=999)), "file changed since scheduling"),
    (snap(file=make_file(modified=1_760_000_001.0)), "file changed since scheduling"),
    (snap(status=head3(present=True, material="PETG", rgb="0000FF")),
     "head 3: expected #FF0000 PETG, found #0000FF PETG"),
    (snap(status=head3(present=True, material="PLA", rgb="FF0000")),
     "head 3: expected #FF0000 PETG, found #FF0000 PLA"),
    (snap(status=head3(present=False)), "head 3: expected #FF0000 PETG, found nothing"),
])
def test_skips(snapshot, reason):
    assert gate(snapshot) == Skip(reason)


def test_starts_when_everything_matches():
    assert gate(snap()) == Start()


def test_material_comparison_ignores_case():
    assert gate(snap(status=head3(present=True, material="petg", rgb="FF0000"))) == Start()


def test_unidentified_loaded_filament_never_matches_identified_expectation():
    snapshot = snap(status=head3(present=True, material=None, rgb=None))
    assert gate(snapshot) == Skip("head 3: expected #FF0000 PETG, found unknown colour, unknown material")


def test_unidentified_expectation_never_matches_unidentified_filament():
    job = make_job(head_map=(HeadAssignment(0, 2, None, None),))
    assert gate(snap(job=job, status=head3(present=True, material=None, rgb=None))) == Skip(
        "head 3: expected unknown colour, unknown material, found unknown colour, unknown material")


def test_same_material_without_colour_does_not_match():
    job = make_job(head_map=(HeadAssignment(0, 2, "PLA", "00FF00"),))
    assert gate(snap(job=job, status=head3(present=True, material="PLA", rgb=None))) == Skip(
        "head 3: expected #00FF00 PLA, found unknown colour, PLA")


def test_empty_head_map_is_skipped():
    job = make_job(head_map=())
    assert gate(snap(job=job)) == Skip("no head assignments (the file has no colour information)")
