from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.printer.client import PrinterOffline
from app.printer.models import PrintState
from app.web.progress import FileEstimates, describe_progress, format_finish, format_span
from tests.factories import make_file, make_status, printing_status
from tests.fakes import FakePrinter

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)


def printing(**overrides):
    values = dict(progress=0.5, print_seconds=840.0, layer=12, total_layers=37)
    values.update(overrides)
    return replace(printing_status(), **values)


def test_nothing_to_show_unless_printing_or_paused():
    assert describe_progress(make_status(), 3600.0, NOW) is None
    assert describe_progress(make_status(state=PrintState.COMPLETE), 3600.0, NOW) is None


def test_time_left_from_the_slicer_estimate():
    view = describe_progress(printing(), 3600.0, NOW)
    assert (view.percent, view.layer, view.total_layers, view.elapsed) == (50, 12, 37, 840.0)
    assert view.left == 2760.0
    assert view.finish_at == NOW + timedelta(seconds=2760)


@pytest.mark.parametrize("estimate", [None, 0.0, 600.0])  # missing, or already overrun
def test_time_left_from_progress_without_a_usable_estimate(estimate):
    view = describe_progress(printing(), estimate, NOW)
    assert view.left == 840.0  # 840 s for half the file: as long again


def test_no_time_left_too_early_to_tell():
    view = describe_progress(printing(progress=0.01, print_seconds=30.0), None, NOW)
    assert view.left is None and view.finish_at is None


def test_paused_print_has_time_left_but_no_finish_time():
    status = printing(state=PrintState.PAUSED, paused=True)
    view = describe_progress(status, 3600.0, NOW)
    assert view.left == 2760.0 and view.finish_at is None


@pytest.mark.parametrize("progress, percent", [(-0.2, 0), (1.7, 100), (0.004, 0), (0.996, 100)])
def test_percent_stays_between_0_and_100(progress, percent):
    assert describe_progress(printing(progress=progress), None, NOW).percent == percent


def test_unknown_print_time_gives_no_time_left():
    view = describe_progress(printing(print_seconds=None), 3600.0, NOW)
    assert view.elapsed is None and view.left is None


def test_finish_time_today_shows_only_the_time():
    berlin = ZoneInfo("Europe/Berlin")
    assert format_finish(NOW + timedelta(minutes=46), NOW, berlin) == "20:46"
    assert format_finish(NOW + timedelta(hours=5), NOW, berlin) == "Thu 01:00"


@pytest.mark.parametrize("seconds, text", [(0, "under a minute"), (59, "under a minute"),
                                           (60, "1 min"), (2760, "46 min"), (3600, "1 h 00 min"),
                                           (5460, "1 h 31 min")])
def test_spans(seconds, text):
    assert format_span(seconds) == text


async def test_estimates_are_read_once_per_file():
    printer = FakePrinter(files={"cube.gcode": make_file(estimated_seconds=1200.0),
                                 "ball.gcode": make_file("ball.gcode", estimated_seconds=None)})
    estimates = FileEstimates(printer)
    assert await estimates.seconds("cube.gcode") == 1200.0
    assert await estimates.seconds("cube.gcode") == 1200.0
    assert await estimates.seconds("ball.gcode") is None
    assert await estimates.seconds("ball.gcode") is None
    assert await estimates.seconds("gone.gcode") is None
    assert printer.calls == [("file_info", "cube.gcode"), ("file_info", "ball.gcode"),
                             ("file_info", "gone.gcode")]


async def test_estimate_read_failure_is_asked_again():
    printer = FakePrinter(files={"cube.gcode": make_file(estimated_seconds=1200.0)})
    printer.fail_on["file_info"] = PrinterOffline("printer unreachable: ConnectError")
    estimates = FileEstimates(printer)
    assert await estimates.seconds("cube.gcode") is None
    del printer.fail_on["file_info"]
    assert await estimates.seconds("cube.gcode") == 1200.0
    assert await estimates.seconds(None) is None


async def test_forgotten_estimate_is_read_again():
    printer = FakePrinter(files={"cube.gcode": make_file(estimated_seconds=1200.0)})
    estimates = FileEstimates(printer)
    await estimates.seconds("cube.gcode")
    estimates.forget()
    printer.files_by_name["cube.gcode"] = make_file(estimated_seconds=900.0)
    assert await estimates.seconds("cube.gcode") == 900.0
