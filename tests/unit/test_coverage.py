from datetime import datetime, timedelta, timezone

from app.bedcheck.coverage import Period, uncovered_gaps

T0 = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
TOL = timedelta(minutes=2)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_fully_watched_window_has_no_gaps():
    assert uncovered_gaps([Period(at(-5), at(60))], at(0), at(30), TOL) == []


def test_long_gap_in_the_middle_is_reported():
    periods = [Period(at(0), at(10)), Period(at(25), at(60))]
    assert uncovered_gaps(periods, at(0), at(30), TOL) == [Period(at(10), at(25))]


def test_gaps_up_to_the_tolerance_are_ignored():
    periods = [Period(at(0), at(10)), Period(at(12), at(60))]
    assert uncovered_gaps(periods, at(0), at(30), TOL) == []


def test_gaps_at_both_ends():
    assert uncovered_gaps([Period(at(5), at(20))], at(0), at(30), TOL) == [
        Period(at(0), at(5)), Period(at(20), at(30))]


def test_overlapping_periods_merge_and_outside_periods_are_ignored():
    periods = [Period(at(-30), at(-20)), Period(at(0), at(15)), Period(at(10), at(30)),
               Period(at(40), at(50))]
    assert uncovered_gaps(periods, at(0), at(30), TOL) == []


def test_no_periods_means_the_whole_window_is_a_gap():
    assert uncovered_gaps([], at(0), at(30), TOL) == [Period(at(0), at(30))]
