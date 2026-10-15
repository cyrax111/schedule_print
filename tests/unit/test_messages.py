from zoneinfo import ZoneInfo

from app.notify import messages
from tests.factories import make_job

BERLIN = ZoneInfo("Europe/Berlin")
JOB = make_job()  # cube.gcode, 2026-10-15 00:00 UTC, Head 3 #FF0000 PETG


def test_job_texts():
    assert messages.job_started(JOB) == "Started cube.gcode (head 3 #FF0000 PETG)"
    assert messages.job_skipped(JOB, "no confirmation", BERLIN) == (
        "Skipped cube.gcode at Thu 15 Oct 2026 02:00 (UTC+02:00): no confirmation")
    assert messages.job_failed(JOB, "printer did not start") == (
        "Failed cube.gcode: printer did not start")
    assert messages.reminder(JOB, "no confirmation", BERLIN) == (
        "cube.gcode is due at Thu 15 Oct 2026 02:00 (UTC+02:00) and the bed is not confirmed "
        "(no confirmation)")


def test_printer_texts():
    assert messages.print_finished("cube.gcode", "complete") == "cube.gcode finished: complete"
    assert messages.print_finished(None, "cancelled") == "Print finished: cancelled"
    assert messages.confirmation_voided("printed since confirmation", 1) == (
        "Bed confirmation voided: printed since confirmation. 1 job waiting.")
    assert messages.confirmation_voided("printed since confirmation", 2).endswith("2 jobs waiting.")
