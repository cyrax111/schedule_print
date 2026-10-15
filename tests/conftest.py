"""Shared test setup. Real printer and Telegram settings never reach a test."""
from datetime import datetime, timezone

import pytest

from app.clock import FakeClock
from app.db import connect, migrate

_REAL_SETTINGS = (
    "PRINTER_HOST", "PRINTER_PORT", "PRINTER_API_KEY", "APP_PASSWORD", "SECRET_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APP_BASE_URL", "DRY_RUN",
)


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch):
    for name in _REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc))


@pytest.fixture
def db():
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()
