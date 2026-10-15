from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.timeutil import (
    LocalTimeError, format_local, format_local_input, local_to_utc, parse_local_input,
)

BERLIN = ZoneInfo("Europe/Berlin")


def test_local_to_utc_in_summer():
    assert local_to_utc(datetime(2026, 7, 1, 2, 0), BERLIN) == datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def test_nonexistent_spring_forward_time_is_rejected():  # Review Focus: bad schedule input
    with pytest.raises(LocalTimeError, match="does not exist"):
        local_to_utc(datetime(2026, 3, 29, 2, 30), BERLIN)


def test_ambiguous_fall_back_time_uses_first_occurrence():
    assert local_to_utc(datetime(2026, 10, 25, 2, 30), BERLIN) == datetime(
        2026, 10, 25, 0, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", ["", "   ", "tomorrow", "2026-13-01T02:00"])
def test_bad_input_is_rejected(text):  # Review Focus: bad schedule input
    with pytest.raises(LocalTimeError):
        parse_local_input(text)


def test_input_with_seconds_is_accepted():
    assert parse_local_input("2026-10-15T02:00:59") == datetime(2026, 10, 15, 2, 0)


def test_formats_show_local_time_and_offset():
    moment = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    assert format_local(moment, BERLIN) == "Thu 15 Oct 2026 02:00 (UTC+02:00)"
    assert format_local_input(moment, BERLIN) == "2026-10-15T02:00"
