from datetime import datetime, timedelta, timezone

from app.clock import FakeClock, SystemClock


def test_system_clock_is_utc_aware():
    assert SystemClock().now().tzinfo is timezone.utc


async def test_fake_clock_moves_only_when_told():
    start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(start)
    await clock.sleep(30)
    clock.advance(timedelta(minutes=1))
    assert clock.now() == start + timedelta(seconds=90)
