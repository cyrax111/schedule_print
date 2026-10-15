from datetime import datetime, timedelta, timezone

import pytest

from app.db import connect, from_db, migrate, to_db


def test_migrate_creates_tables_once():
    conn = connect(":memory:")
    assert migrate(conn) == 1
    assert migrate(conn) == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"confirmations", "print_events", "watch_periods", "jobs", "notifications"} <= tables


def test_timestamps_round_trip_as_utc():
    local = datetime(2026, 10, 15, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    stored = to_db(local)
    assert stored == "2026-10-15T00:00:00.000000+00:00"
    assert from_db(stored) == local
    assert from_db(None) is None


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        to_db(datetime(2026, 1, 1))


def test_stored_timestamps_sort_in_time_order():
    a = to_db(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
    b = to_db(datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc))
    c = to_db(datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
    assert a < b < c
