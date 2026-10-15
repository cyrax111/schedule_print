# Scheduler Core (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A web app on an always-on home box that starts Snapmaker U1 prints at scheduled times, only when a bed-clear confirmation is still valid and the loaded filament matches, with notifications on the page and in Telegram.

**Architecture:** One Python process. FastAPI serves Jinja2 pages with htmx; background asyncio tasks run a Moonraker websocket watcher, the job runner and Telegram delivery; SQLite stores confirmations, watch periods, print events, jobs and notifications. Everything that knows about Moonraker or the U1 lives in `app/printer/`; the start/skip decision is a pure function (`app/jobs/gate.py`).

**Tech Stack:** Python 3.12, FastAPI, Starlette sessions, Jinja2, htmx 2.0.3 (vendored), httpx, websockets ≥14, stdlib sqlite3, pytest + pytest-asyncio, Docker.

**Spec:** `docs/superpowers/specs/2026-10-14-schedule-print-design.md`

**Order of work:**
1. `docs/superpowers/plans/2026-10-14-u1-printer-probe.md` (probe). Its findings note says GO or STOP.
2. This plan, Tasks 1–21. Task 3 applies the probe's "Changes needed" list; Task 20 tests against the probe's fixtures; Task 21 is the acceptance run on the real printer.

## Global Constraints

- Python ≥ 3.12. Dependencies only as listed in `pyproject.toml` (Task 1).
- Only `app/printer/` knows Moonraker or U1 details (spec §5.1).
- Timestamps are stored in UTC as ISO-8601 with microseconds; pages show them in `TZ` with the UTC offset.
- Heads are 0–3 in code and "Head 1"–"Head 4" in text (= T0–T3).
- Defaults: `OFFLINE_GRACE_MINUTES=10`, `START_VERIFY_SECONDS=90`, `REMINDER_MINUTES=60`; gap tolerance 2 min; watcher heartbeat 30 s; reconnect backoff 1 s doubling to 30 s; runner tick ≤ 30 s; status poll 2 s; Telegram retries 10 s, 30 s, 1 min, 5 min, 15 min; login lockout after 5 wrong passwords for 60 s.
- Never start a job late (more than `OFFLINE_GRACE_MINUTES` after its time). When the app cannot prove the printer stayed idle, the job is skipped.
- Tests never contact a real printer or the real Telegram API.
- A notification problem never affects scheduling.
- The Telegram bot token never reaches logs or the database.
- Commands assume the virtualenv is active: `source .venv/bin/activate`.

## Review Focus

- **Unusual file names.** Spaces, non-ASCII characters, subfolders or HTML characters (e.g. `plates/globe light_PETG_5h56m.gcode`, `<b>x</b>.gcode`) must be URL-encoded in every printer request and HTML-escaped on every page. Tests: Task 4 `test_file_info_reads_gcode_tail_when_metadata_lacks_colours`, Task 16 `test_files_page_lists_files_with_colours_and_encoded_links`, Task 17 `test_jobs_page_escapes_file_names`.
- **Half-reported head slots.** `"NONE"`, empty strings, missing arrays or short colour values must show as empty or unknown, never crash. Test: Task 3 `test_half_reported_heads_do_not_crash`.
- **Printer errors.** If the printer answers with an error, an HTML page, or times out while a page or the runner needs it, show a readable message or skip the job with a reason. Never a 500 and never a stuck job. Tests: Task 4 `test_server_error_page_becomes_printer_error`, Task 13 `test_unreadable_file_skips_with_reason`, Task 16 `test_printer_offline_shows_message`, Task 17 `test_offline_printer_blocks_scheduling`.
- **Bad schedule input.** An empty or malformed time, a time in the past, or a time skipped by a clock change must be a form error. Tests: Task 8 `test_bad_input_is_rejected` and `test_nonexistent_spring_forward_time_is_rejected`, Task 17 `test_bad_times_are_form_errors`.
- **Acting on a finished job.** Editing or cancelling a job that already ran or was cancelled (stale tab, double submit) must give a clear 409 message and change nothing. Tests: Task 8 `test_update_resets_reminder_and_only_for_pending`, Task 17 `test_acting_on_finished_job_is_refused`.

## File Structure

```
pyproject.toml                 dependencies, package data, pytest config
Dockerfile, docker-compose.yml, .env.example, README.md
docker-compose.test.yml, scripts/sim-up.sh        u1sim for integration tests
app/
  config.py        Settings from environment; secret key file
  clock.py         Clock protocol, SystemClock, FakeClock
  db.py            sqlite connection, migrations, UTC timestamp helpers
  migrations/001_initial.sql
  timeutil.py      local ↔ UTC, DST checks, display formats
  main.py          wiring, lifespan tasks, logging, uvicorn factory
  printer/         ── only place that knows Moonraker / U1 ──
    models.py      PrinterStatus, HeadFilament, FileInfo, FileColour, HistoryJob…
    u1.py          U1 facts (objects, idle states) and pure parsers
    client.py      PrinterClient (HTTP)
    events.py      event types and EventBus
    watcher.py     StateTracker and PrinterWatcher (websocket)
  bedcheck/
    coverage.py    uncovered_gaps (pure)
    service.py     BedCheck: confirmation rule, watch periods, print events
  jobs/
    models.py      Job, JobStatus, HeadAssignment
    store.py       JobStore (SQLite)
    heads.py       head suggestion, assignment building, filament text
    gate.py        gate() start/skip decision (pure)
    runner.py      Runner: due jobs, reminders, start sequence, recovery
  notify/
    messages.py    notification texts (pure)
    service.py     Notifier: feed rows, unread, Telegram queue state
    telegram.py    TelegramSender, DeliveryWorker, redaction
  web/
    app.py         Services, create_app, templates
    auth.py        password login, session, lockout, origin check
    routes_dashboard.py, routes_notifications.py, routes_files.py, routes_jobs.py
    templates/*.html, static/app.css, static/htmx.min.js
tests/
  conftest.py, factories.py, fakes.py
  unit/…, web/…, integration/…, contract/…
  fixtures/two_colour.gcode, fixtures/real_u1/… (probe), fixtures/u1sim/…
```

---

### Task 1: Project skeleton, settings and clock

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `app/__init__.py`, `app/config.py`, `app/clock.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/unit/__init__.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_clock.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.config.Settings` (frozen dataclass) with fields `printer_host, app_password, printer_port=7125, printer_api_key=None, secret_key=None, tz="UTC", dry_run=False, offline_grace_minutes=10, start_verify_seconds=90, reminder_minutes=60, data_dir=Path("/data"), telegram_bot_token=None, telegram_chat_id=None, app_base_url=None`; properties `printer_base_url`, `printer_ws_url`, `telegram_enabled`; `Settings.from_env(env: Mapping[str, str] | None = None) -> Settings`.
  - `app.config.ConfigError(ValueError)` and `load_or_create_secret(settings: Settings) -> str`.
  - `app.clock.Clock` (protocol: `now() -> datetime` aware UTC, `async sleep(seconds: float)`), `SystemClock`, `FakeClock(start)` with `advance(delta: float | timedelta)`.
  - Test fixture `clock` → `FakeClock(2026-10-14 18:00 UTC)`.

- [ ] **Step 1: Create the project files and install**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "schedule-print"
version = "0.1.0"
description = "Scheduled, bed-confirmed printing for a Snapmaker U1"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115,<1",
    "uvicorn[standard]>=0.30",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "httpx>=0.27,<1",
    "websockets>=14",
    "itsdangerous>=2.2",
    "tzdata>=2024.1",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.setuptools.package-data]
app = ["migrations/*.sql", "web/templates/*.html", "web/static/*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["."]
markers = ["integration: needs the u1sim simulator (see tests/integration/README.md)"]
addopts = "-m 'not integration'"
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
data/
.env
vendor/
```

Create empty `app/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`.

`tests/conftest.py`:

```python
"""Shared test setup. Real printer and Telegram settings never reach a test."""
from datetime import datetime, timezone

import pytest

from app.clock import FakeClock

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
```

Run:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[dev]'
```

Expected: install finishes without errors.

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_config.py`:

```python
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, load_or_create_secret

BASE = {"PRINTER_HOST": "192.168.1.50", "APP_PASSWORD": "pw"}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.printer_base_url == "http://192.168.1.50:7125"
    assert s.printer_ws_url == "ws://192.168.1.50:7125/websocket"
    assert (s.offline_grace_minutes, s.start_verify_seconds, s.reminder_minutes) == (10, 90, 60)
    assert s.tz == "UTC" and s.dry_run is False and s.data_dir == Path("/data")
    assert s.telegram_enabled is False


def test_missing_required():
    with pytest.raises(ConfigError, match="PRINTER_HOST, APP_PASSWORD"):
        Settings.from_env({})


def test_unknown_time_zone():
    with pytest.raises(ConfigError, match="TZ"):
        Settings.from_env({**BASE, "TZ": "Mars/Olympus"})


@pytest.mark.parametrize("raw, expected", [("yes", True), ("1", True), ("false", False), ("", False)])
def test_dry_run_parsing(raw, expected):
    assert Settings.from_env({**BASE, "DRY_RUN": raw}).dry_run is expected


def test_bad_numbers():
    with pytest.raises(ConfigError, match="REMINDER_MINUTES"):
        Settings.from_env({**BASE, "REMINDER_MINUTES": "0"})
    with pytest.raises(ConfigError, match="PRINTER_PORT"):
        Settings.from_env({**BASE, "PRINTER_PORT": "abc"})


def test_telegram_needs_both_values():
    assert not Settings.from_env({**BASE, "TELEGRAM_BOT_TOKEN": "t"}).telegram_enabled
    both = {**BASE, "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    assert Settings.from_env(both).telegram_enabled


def test_secret_is_created_once(tmp_path):
    s = Settings.from_env({**BASE, "DATA_DIR": str(tmp_path)})
    first = load_or_create_secret(s)
    assert len(first) >= 32
    assert load_or_create_secret(s) == first
    assert load_or_create_secret(Settings.from_env({**BASE, "SECRET_KEY": "given"})) == "given"
```

`tests/unit/test_clock.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit -v`
Expected: collection errors, `ModuleNotFoundError: No module named 'app.config'` (and `app.clock` from conftest).

- [ ] **Step 4: Implement settings and clock**

`app/config.py`:

```python
"""Settings read from environment variables."""
from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """A setting is missing or invalid."""


def _bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _positive_int(name: str, value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {number}")
    return number


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    printer_host: str
    app_password: str
    printer_port: int = 7125
    printer_api_key: str | None = None
    secret_key: str | None = None
    tz: str = "UTC"
    dry_run: bool = False
    offline_grace_minutes: int = 10
    start_verify_seconds: int = 90
    reminder_minutes: int = 60
    data_dir: Path = Path("/data")
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    app_base_url: str | None = None

    @property
    def printer_base_url(self) -> str:
        return f"http://{self.printer_host}:{self.printer_port}"

    @property
    def printer_ws_url(self) -> str:
        return f"ws://{self.printer_host}:{self.printer_port}/websocket"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [n for n in ("PRINTER_HOST", "APP_PASSWORD") if not env.get(n, "").strip()]
        if missing:
            raise ConfigError(f"missing required setting(s): {', '.join(missing)}")
        tz = env.get("TZ", "UTC").strip() or "UTC"
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"TZ is not a known time zone: {tz!r}") from None
        return cls(
            printer_host=env["PRINTER_HOST"].strip(),
            app_password=env["APP_PASSWORD"],
            printer_port=_positive_int("PRINTER_PORT", env.get("PRINTER_PORT", "7125")),
            printer_api_key=_optional(env, "PRINTER_API_KEY"),
            secret_key=_optional(env, "SECRET_KEY"),
            tz=tz,
            dry_run=_bool("DRY_RUN", env.get("DRY_RUN", "false")),
            offline_grace_minutes=_positive_int(
                "OFFLINE_GRACE_MINUTES", env.get("OFFLINE_GRACE_MINUTES", "10")),
            start_verify_seconds=_positive_int(
                "START_VERIFY_SECONDS", env.get("START_VERIFY_SECONDS", "90")),
            reminder_minutes=_positive_int("REMINDER_MINUTES", env.get("REMINDER_MINUTES", "60")),
            data_dir=Path(env.get("DATA_DIR", "/data")),
            telegram_bot_token=_optional(env, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_optional(env, "TELEGRAM_CHAT_ID"),
            app_base_url=_optional(env, "APP_BASE_URL"),
        )


def load_or_create_secret(settings: Settings) -> str:
    """Session-signing key: SECRET_KEY if set, else a key generated once into DATA_DIR."""
    if settings.secret_key:
        return settings.secret_key
    path = settings.data_dir / "secret_key"
    if path.exists():
        return path.read_text().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    path.write_text(secret)
    path.chmod(0o600)
    return secret
```

`app/clock.py`:

```python
"""Time source, injected so tests can control it."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Clock for tests: time moves only when advanced or slept."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: float | timedelta) -> None:
        self._now += delta if isinstance(delta, timedelta) else timedelta(seconds=delta)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all tests in `test_config.py` and `test_clock.py` PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore app tests
git commit -m "feat: project skeleton with settings and clock"
```

---

### Task 2: Database and migrations

**Files:**
- Create: `app/db.py`, `app/migrations/001_initial.sql`
- Modify: `tests/conftest.py` (add `db` fixture)
- Test: `tests/unit/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.db.connect(path: str | Path) -> sqlite3.Connection` (autocommit, `Row` rows, `check_same_thread=False`), `migrate(conn) -> int` (returns schema version), `to_db(dt: datetime) -> str`, `from_db(value: str | None) -> datetime | None`. Tables `confirmations`, `print_events`, `watch_periods`, `jobs`, `notifications` exactly as in Step 3. Test fixture `db` → migrated in-memory connection.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_db.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`.

- [ ] **Step 3: Implement the database module and schema**

`app/migrations/001_initial.sql`:

```sql
CREATE TABLE confirmations (
    id INTEGER PRIMARY KEY,
    confirmed_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    consumed_by_job_id INTEGER
);

CREATE TABLE print_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('started', 'finished')),
    filename TEXT,
    result TEXT,
    source TEXT NOT NULL CHECK (source IN ('live', 'history'))
);

CREATE TABLE watch_periods (
    id INTEGER PRIMARY KEY,
    began_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_modified REAL NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    head_map_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'starting', 'started', 'skipped', 'failed', 'cancelled')),
    outcome_reason TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX jobs_status_time ON jobs (status, scheduled_at_utc);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    job_id INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    telegram_status TEXT NOT NULL CHECK (telegram_status IN ('off', 'pending', 'sent', 'failed')),
    telegram_attempts INTEGER NOT NULL DEFAULT 0,
    telegram_error TEXT,
    telegram_next_at TEXT
);
```

`app/db.py`:

```python
"""SQLite connection, schema migrations and timestamp helpers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    # Autocommit; one connection shared by the event loop and the test client thread.
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migrations() -> list[tuple[int, str]]:
    folder = resources.files("app") / "migrations"
    found = [
        (int(entry.name.split("_", 1)[0]), entry.read_text())
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in _migrations():
        if number > current:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {number}")
            current = number
    return current


def to_db(moment: datetime) -> str:
    """Fixed-width UTC text, so stored values sort in time order."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; use an aware UTC datetime")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_db(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
```

Add to `tests/conftest.py`:

```python
from app.db import connect, migrate


@pytest.fixture
def db():
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/migrations tests/conftest.py tests/unit/test_db.py
git commit -m "feat: sqlite schema, migrations and UTC timestamp helpers"
```

---

### Task 3: Printer models and U1 parsing

**Files:**
- Create: `app/printer/__init__.py`, `app/printer/models.py`, `app/printer/u1.py`, `tests/factories.py`
- Test: `tests/unit/test_u1_parsing.py`

**Interfaces:**
- Consumes: `docs/superpowers/probe/u1-findings.md` → "Changes needed" (apply them to the marked constants in `u1.py`).
- Produces:
  - `app.printer.models`: `HEAD_COUNT = 4`; `PrintState` (StrEnum: standby, printing, paused, complete, cancelled, error, unknown); frozen dataclasses `HeadFilament(head, present, material=None, vendor=None, rgb=None)`, `Temperature(current, target)`, `PrinterStatus(online, klippy_ready=False, state=UNKNOWN, idle=False, filename=None, progress=0.0, paused=False, exception=None, bed=Temperature(None, None), nozzles=(), heads=())` with `PrinterStatus.offline()`, `FileColour(logical, rgb, material)`, `FileInfo(filename, size, modified, estimated_seconds=None, colours=(), thumbnail=None)`, `FileEntry(filename, size, modified)`, `HistoryJob(filename, start_time, status)`.
  - `app.printer.u1`: `EXTRUDER_OBJECTS`, `SUBSCRIBE_OBJECTS`, `IDLE_STATES`, `normalise_rgb`, `parse_heads`, `parse_exception`, `parse_state`, `parse_status(objects) -> PrinterStatus`, `parse_gcode_comments(text) -> dict[str, str]`, `parse_file_colours(meta, comments) -> tuple[FileColour, ...]`, `needs_gcode_tail(meta) -> bool`, `parse_file_info(filename, meta, comments) -> FileInfo`, `head_map_script(assignments) -> str`, `head_map_matches(ptc, assignments) -> bool`, `parse_history(result) -> list[HistoryJob]`.
  - `tests.factories`: `IDLE_OBJECTS`, `with_changes(**objects)`, `DEFAULT_HEADS` (Head 1 white PLA, Head 2 black PLA, Head 3 red PETG, Head 4 empty), `make_status(**overrides)`, `printing_status(filename="cube.gcode")`, `make_file(filename="cube.gcode", **overrides)` (size 1000, modified 1_760_000_000.0, 1 h, one white PLA colour).

- [ ] **Step 1: Write the test data builders**

Create empty `app/printer/__init__.py`, then `tests/factories.py`:

```python
"""Small builders for test data."""
from app.printer.models import (
    FileColour, FileInfo, HeadFilament, PrinterStatus, PrintState, Temperature,
)

IDLE_OBJECTS = {
    "webhooks": {"state": "ready"},
    "print_stats": {"state": "standby", "filename": ""},
    "virtual_sdcard": {"progress": 0.0},
    "pause_resume": {"is_paused": False},
    "heater_bed": {"temperature": 24.5, "target": 0.0},
    "extruder": {"temperature": 25.0, "target": 0.0},
    "extruder1": {"temperature": 25.1, "target": 0.0},
    "extruder2": {"temperature": 25.2, "target": 0.0},
    "extruder3": {"temperature": 25.3, "target": 0.0},
    "exception_manager": {"exceptions": []},
    "print_task_config": {
        "filament_exist": [True, True, True, False],
        "filament_type": ["PLA", "PETG", "PLA", "NONE"],
        "filament_vendor": ["Snapmaker", "Generic", "Polymaker", "NONE"],
        "filament_color_rgba": ["FFFFFFFF", "000000FF", "f78e0eff", "NONE"],
        "extruder_map_table": [0, 1, 2, 3] + [0] * 28,
    },
}


def with_changes(**objects) -> dict:
    merged = {name: dict(fields) for name, fields in IDLE_OBJECTS.items()}
    for name, fields in objects.items():
        merged.setdefault(name, {}).update(fields)
    return merged


DEFAULT_HEADS = (
    HeadFilament(0, True, "PLA", "Snapmaker", "FFFFFF"),
    HeadFilament(1, True, "PLA", "Snapmaker", "000000"),
    HeadFilament(2, True, "PETG", "Generic", "FF0000"),
    HeadFilament(3, False),
)


def make_status(**overrides) -> PrinterStatus:
    values = dict(
        online=True, klippy_ready=True, state=PrintState.STANDBY, idle=True,
        bed=Temperature(25.0, 0.0), nozzles=(Temperature(25.0, 0.0),) * 4, heads=DEFAULT_HEADS,
    )
    values.update(overrides)
    return PrinterStatus(**values)


def printing_status(filename: str = "cube.gcode") -> PrinterStatus:
    return make_status(state=PrintState.PRINTING, idle=False, filename=filename, progress=0.1)


def make_file(filename: str = "cube.gcode", **overrides) -> FileInfo:
    values = dict(
        filename=filename, size=1000, modified=1_760_000_000.0, estimated_seconds=3600.0,
        colours=(FileColour(0, "FFFFFF", "PLA"),), thumbnail=None,
    )
    values.update(overrides)
    return FileInfo(**values)
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_u1_parsing.py`:

```python
from datetime import datetime, timezone

from app.printer import u1
from app.printer.models import FileColour, PrintState
from tests.factories import IDLE_OBJECTS, with_changes


def test_idle_printer():
    status = u1.parse_status(IDLE_OBJECTS)
    assert status.online and status.klippy_ready and status.idle
    assert status.state is PrintState.STANDBY
    assert status.filename is None
    assert [h.rgb for h in status.heads] == ["FFFFFF", "000000", "F78E0E", None]
    assert [h.present for h in status.heads] == [True, True, True, False]
    assert status.heads[3].material is None
    assert status.nozzles[2].current == 25.2
    assert status.bed.current == 24.5


def test_printing_is_not_idle():
    status = u1.parse_status(with_changes(print_stats={"state": "printing", "filename": "cube.gcode"}))
    assert status.state is PrintState.PRINTING and not status.idle
    assert status.filename == "cube.gcode"


def test_paused_flag_is_not_idle():
    assert not u1.parse_status(with_changes(pause_resume={"is_paused": True})).idle


def test_active_exception_is_not_idle():
    status = u1.parse_status(with_changes(
        exception_manager={"exceptions": [{"code": 3, "message": "Nozzle clog"}]}))
    assert status.exception == "Nozzle clog" and not status.idle


def test_klippy_not_ready_is_not_idle():
    assert not u1.parse_status(with_changes(webhooks={"state": "startup"})).idle


def test_unknown_state_is_not_idle():
    status = u1.parse_status(with_changes(print_stats={"state": "calibrating"}))
    assert status.state is PrintState.UNKNOWN and not status.idle


def test_half_reported_heads_do_not_crash():  # Review Focus: half-reported head slots
    status = u1.parse_status(with_changes(print_task_config={
        "filament_exist": [True],
        "filament_type": ["", "NONE"],
        "filament_color_rgba": ["12", None],
        "filament_vendor": None,
    }))
    assert len(status.heads) == 4
    first = status.heads[0]
    assert first.present and first.material is None and first.rgb is None
    assert not any(h.present for h in status.heads[1:])


def test_missing_task_config_gives_four_empty_heads():
    objects = with_changes()
    del objects["print_task_config"]
    assert [h.present for h in u1.parse_status(objects).heads] == [False] * 4


def test_colours_from_metadata_lists():
    meta = {"filament_colors": ["#FF0000", "#00FF00", "#0000FF"],
            "filament_type": "PLA;PETG;PLA", "referenced_tools": [0, 2]}
    assert u1.parse_file_colours(meta, {}) == (
        FileColour(0, "FF0000", "PLA"), FileColour(2, "0000FF", "PLA"))


def test_colours_from_gcode_comments_skip_unused_filaments():
    comments = u1.parse_gcode_comments(
        "; filament used [mm] = 120.50, 0.00, 30.2\n"
        "; filament_colour = #FFFFFF;#000000;#ff0000\n"
        "; filament_type = PLA;PLA;PETG\n"
    )
    assert u1.parse_file_colours({}, comments) == (
        FileColour(0, "FFFFFF", "PLA"), FileColour(2, "FF0000", "PETG"))


def test_file_without_colour_information_has_one_unknown_colour():
    assert u1.parse_file_colours({}, {}) == (FileColour(0, None, None),)


def test_needs_tail_only_when_metadata_is_incomplete():
    assert u1.needs_gcode_tail({})
    assert u1.needs_gcode_tail({"filament_colors": ["#FFFFFF"]})
    assert not u1.needs_gcode_tail({"filament_colors": ["#FFFFFF"], "referenced_tools": [0]})


def test_file_info_picks_largest_thumbnail_in_subfolder():
    meta = {
        "size": 2048, "modified": 1760000000.5, "estimated_time": 3725,
        "thumbnails": [
            {"width": 32, "height": 32, "relative_path": ".thumbs/a-32x32.png"},
            {"width": 300, "height": 300, "relative_path": ".thumbs/a-300x300.png"},
        ],
        "filament_colors": ["#FFFFFF"], "filament_type": "PLA", "referenced_tools": [0],
    }
    info = u1.parse_file_info("plates/a b.gcode", meta, {})
    assert info.thumbnail == "plates/.thumbs/a-300x300.png"
    assert (info.size, info.modified, info.estimated_seconds) == (2048, 1760000000.5, 3725.0)
    assert info.colours == (FileColour(0, "FFFFFF", "PLA"),)


def test_head_map_script_and_check():
    assert u1.head_map_script([(0, 2), (1, 0)]) == (
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=2\n"
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=1 MAP_EXTRUDER=0"
    )
    table = {"extruder_map_table": [2, 0] + [0] * 30}
    assert u1.head_map_matches(table, [(0, 2), (1, 0)])
    assert not u1.head_map_matches(table, [(0, 1)])
    assert not u1.head_map_matches({}, [(0, 2)])


def test_history_parsing_skips_entries_without_start_time():
    jobs = u1.parse_history({"jobs": [
        {"filename": "a.gcode", "start_time": 1760000000.0, "status": "completed"},
        {"filename": "broken"},
    ]})
    assert len(jobs) == 1
    assert jobs[0].start_time == datetime.fromtimestamp(1760000000.0, timezone.utc)
    assert (jobs[0].filename, jobs[0].status) == ("a.gcode", "completed")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_u1_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.printer.models'`.

- [ ] **Step 4: Implement the models**

`app/printer/models.py`:

```python
"""Printer data as the rest of the app sees it, independent of Moonraker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

HEAD_COUNT = 4


class PrintState(StrEnum):
    STANDBY = "standby"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HeadFilament:
    head: int                 # 0-3; shown to people as head + 1
    present: bool
    material: str | None = None
    vendor: str | None = None
    rgb: str | None = None    # "RRGGBB", upper case


@dataclass(frozen=True)
class Temperature:
    current: float | None
    target: float | None


@dataclass(frozen=True)
class PrinterStatus:
    online: bool
    klippy_ready: bool = False
    state: PrintState = PrintState.UNKNOWN
    idle: bool = False
    filename: str | None = None
    progress: float = 0.0
    paused: bool = False
    exception: str | None = None
    bed: Temperature = Temperature(None, None)
    nozzles: tuple[Temperature, ...] = ()
    heads: tuple[HeadFilament, ...] = ()

    @classmethod
    def offline(cls) -> PrinterStatus:
        return cls(online=False)


@dataclass(frozen=True)
class FileColour:
    logical: int              # colour number in the sliced file, 0-based
    rgb: str | None
    material: str | None


@dataclass(frozen=True)
class FileInfo:
    filename: str
    size: int
    modified: float
    estimated_seconds: float | None = None
    colours: tuple[FileColour, ...] = ()
    thumbnail: str | None = None   # path relative to the gcodes root


@dataclass(frozen=True)
class FileEntry:
    filename: str
    size: int
    modified: float


@dataclass(frozen=True)
class HistoryJob:
    filename: str | None
    start_time: datetime
    status: str
```

- [ ] **Step 5: Implement the U1 parsers**

Read `docs/superpowers/probe/u1-findings.md` first. For every item under "Changes needed", change the matching `PROBE` constant below before saving the file.

`app/printer/u1.py`:

```python
"""Everything specific to the Snapmaker U1 and its Moonraker fork.

Constants marked PROBE come from community research and are checked against
the real printer in docs/superpowers/probe/u1-findings.md.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .models import (
    HEAD_COUNT, FileColour, FileInfo, HeadFilament, HistoryJob, PrinterStatus, PrintState,
    Temperature,
)

EXTRUDER_OBJECTS = ("extruder", "extruder1", "extruder2", "extruder3")

# PROBE Q2: objects and fields the watcher subscribes to (None = all fields).
SUBSCRIBE_OBJECTS: dict[str, list[str] | None] = {
    "webhooks": ["state"],
    "print_stats": ["state", "filename"],
    "virtual_sdcard": ["progress"],
    "pause_resume": ["is_paused"],
    "heater_bed": ["temperature", "target"],
    **{name: ["temperature", "target"] for name in EXTRUDER_OBJECTS},
    "exception_manager": None,
    "print_task_config": [
        "filament_type", "filament_vendor", "filament_color_rgba", "filament_exist",
        "extruder_map_table",
    ],
}

# PROBE Q3: print_stats states in which the printer may be confirmed or started.
# If 'error' turns out to persist after a failed print, add PrintState.ERROR here;
# active faults are still caught through exception_manager.
IDLE_STATES = frozenset({PrintState.STANDBY, PrintState.COMPLETE, PrintState.CANCELLED})

# PROBE Q5: metadata keys tried before falling back to G-code comments.
META_COLOUR_KEYS = ("filament_colors", "filament_colour")
META_TYPE_KEYS = ("filament_type",)
COMMENT_COLOUR_KEY = "filament_colour"
COMMENT_TYPE_KEY = "filament_type"
COMMENT_USED_KEY = "filament used [mm]"

_EMPTY = {"", "NONE", "NULL"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.upper() in _EMPTY else text


def _index(seq: Any, i: int) -> Any:
    if isinstance(seq, (list, tuple)) and i < len(seq):
        return seq[i]
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_rgb(value: Any) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    text = text.lstrip("#").upper()
    if len(text) >= 6 and all(c in "0123456789ABCDEF" for c in text[:6]):
        return text[:6]
    return None


def parse_heads(ptc: Mapping[str, Any] | None) -> tuple[HeadFilament, ...]:
    ptc = ptc or {}
    return tuple(
        HeadFilament(
            head=i,
            present=bool(_index(ptc.get("filament_exist"), i)),
            material=_clean(_index(ptc.get("filament_type"), i)),
            vendor=_clean(_index(ptc.get("filament_vendor"), i)),
            rgb=normalise_rgb(_index(ptc.get("filament_color_rgba"), i)),
        )
        for i in range(HEAD_COUNT)
    )


def parse_exception(obj: Mapping[str, Any] | None) -> str | None:
    if not obj:
        return None
    exceptions = obj.get("exceptions")
    if not exceptions:
        return None
    first = exceptions[0] if isinstance(exceptions, list) else exceptions
    if isinstance(first, Mapping):
        for key in ("message", "msg", "description", "code"):
            if first.get(key) not in (None, ""):
                return str(first[key])
    return str(first)


def parse_state(value: Any) -> PrintState:
    try:
        return PrintState(str(value))
    except ValueError:
        return PrintState.UNKNOWN


def _temperature(obj: Mapping[str, Any] | None) -> Temperature:
    obj = obj or {}
    return Temperature(_float(obj.get("temperature")), _float(obj.get("target")))


def parse_status(objects: Mapping[str, Mapping[str, Any]]) -> PrinterStatus:
    stats = objects.get("print_stats") or {}
    state = parse_state(stats.get("state"))
    klippy_ready = (objects.get("webhooks") or {}).get("state") == "ready"
    paused = bool((objects.get("pause_resume") or {}).get("is_paused"))
    exception = parse_exception(objects.get("exception_manager"))
    return PrinterStatus(
        online=True,
        klippy_ready=klippy_ready,
        state=state,
        idle=klippy_ready and state in IDLE_STATES and not paused and exception is None,
        filename=_clean(stats.get("filename")),
        progress=_float((objects.get("virtual_sdcard") or {}).get("progress")) or 0.0,
        paused=paused,
        exception=exception,
        bed=_temperature(objects.get("heater_bed")),
        nozzles=tuple(_temperature(objects.get(name)) for name in EXTRUDER_OBJECTS),
        heads=parse_heads(objects.get("print_task_config")),
    )


def parse_gcode_comments(text: str) -> dict[str, str]:
    """Collect `; key = value` lines. Later lines win (Orca's config block is last)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(";") and "=" in line:
            key, _, value = line[1:].partition("=")
            found[key.strip()] = value.strip()
    return found


def _split(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value]
    text = str(value).strip()
    if not text:
        return []
    separator = ";" if ";" in text else ","
    return [part.strip() for part in text.split(separator)]


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if mapping.get(key) not in (None, "", []):
            return mapping[key]
    return None


def _used_flags(meta: Mapping[str, Any], comments: Mapping[str, str], count: int) -> list[bool] | None:
    tools = meta.get("referenced_tools")
    if isinstance(tools, list) and tools:
        return [i in tools for i in range(count)]
    used = comments.get(COMMENT_USED_KEY)
    if used:
        flags = []
        for part in _split(used):
            value = _float(part)
            flags.append(True if value is None else value > 0)
        return flags
    return None


def parse_file_colours(meta: Mapping[str, Any], comments: Mapping[str, str]) -> tuple[FileColour, ...]:
    colours = _split(_first(meta, META_COLOUR_KEYS) or comments.get(COMMENT_COLOUR_KEY))
    types = _split(_first(meta, META_TYPE_KEYS) or comments.get(COMMENT_TYPE_KEY))
    count = max(len(colours), len(types))
    if count == 0:
        return (FileColour(0, None, None),)

    def colour(i: int) -> FileColour:
        return FileColour(
            i,
            normalise_rgb(colours[i]) if i < len(colours) else None,
            _clean(types[i]) if i < len(types) else None,
        )

    used = _used_flags(meta, comments, count)
    result = tuple(colour(i) for i in range(count) if used is None or (i < len(used) and used[i]))
    return result or (colour(0),)


def needs_gcode_tail(meta: Mapping[str, Any]) -> bool:
    return _first(meta, META_COLOUR_KEYS) is None or not meta.get("referenced_tools")


def parse_file_info(filename: str, meta: Mapping[str, Any], comments: Mapping[str, str]) -> FileInfo:
    thumbnail = None
    thumbs = meta.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        relative = best.get("relative_path")
        if relative:
            folder = filename.rpartition("/")[0]
            thumbnail = f"{folder}/{relative}" if folder else relative
    return FileInfo(
        filename=filename,
        size=int(meta.get("size") or 0),
        modified=float(meta.get("modified") or 0.0),
        estimated_seconds=_float(meta.get("estimated_time")),
        colours=parse_file_colours(meta, comments),
        thumbnail=thumbnail,
    )


def head_map_script(assignments: Iterable[tuple[int, int]]) -> str:
    return "\n".join(
        f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={logical} MAP_EXTRUDER={head}"
        for logical, head in assignments
    )


def head_map_matches(ptc: Mapping[str, Any] | None, assignments: Iterable[tuple[int, int]]) -> bool:
    # PROBE Q7: extruder_map_table is a list indexed by logical colour holding the head.
    table = (ptc or {}).get("extruder_map_table")
    if not isinstance(table, list):
        return False
    return all(logical < len(table) and _as_int(table[logical]) == head
               for logical, head in assignments)


def parse_history(result: Mapping[str, Any]) -> list[HistoryJob]:
    jobs = []
    for item in result.get("jobs") or []:
        start = _float(item.get("start_time"))
        if start is None:
            continue
        jobs.append(HistoryJob(
            filename=item.get("filename"),
            start_time=datetime.fromtimestamp(start, timezone.utc),
            status=str(item.get("status", "")),
        ))
    return jobs
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/printer tests/factories.py tests/unit/test_u1_parsing.py
git commit -m "feat: printer models and U1 status, filament and file parsers"
```

---

### Task 4: Printer HTTP client

**Files:**
- Create: `app/printer/client.py`, `tests/fakes.py`
- Test: `tests/unit/test_printer_client.py`

**Interfaces:**
- Consumes: `app.printer.u1` parsers and `SUBSCRIBE_OBJECTS` (Task 3).
- Produces:
  - `PrinterError(Exception)`, `PrinterOffline(PrinterError)`.
  - `PrinterClient(base_url: str, api_key: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0)` with async methods `close()`, `query(objects: dict[str, list[str] | None]) -> dict`, `status() -> PrinterStatus` (offline status instead of raising), `files() -> list[FileEntry]` (G-code only, newest first), `file_info(filename) -> FileInfo | None` (cached per filename and modified time), `upload(filename, content: bytes) -> None`, `run_gcode(script) -> None`, `apply_head_map(assignments: Sequence[tuple[int, int]]) -> None` (raises `PrinterError` if read-back differs), `start_print(filename) -> None`, `history(since: datetime) -> list[HistoryJob] | None`, `thumbnail(path) -> bytes | None`.
  - `tests.fakes.Router` (callable for `httpx.MockTransport`, records `.requests`) and `ok(result, status=200) -> httpx.Response`.

- [ ] **Step 1: Write the test helpers**

`tests/fakes.py`:

```python
"""Fakes for printer, websocket and Telegram used across tests."""
from __future__ import annotations

from collections.abc import Callable

import httpx


def ok(result, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"result": result})


class Router:
    """httpx.MockTransport handler: routes by (method, decoded path) and records requests."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response | Callable[[httpx.Request], httpx.Response]]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return handler(request) if callable(handler) else handler
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_printer_client.py`:

```python
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.printer.client import PrinterClient, PrinterError, PrinterOffline
from app.printer.models import FileColour
from tests.factories import IDLE_OBJECTS
from tests.fakes import Router, ok


def client_for(routes) -> tuple[PrinterClient, Router]:
    router = Router(routes)
    return PrinterClient("http://printer:7125", transport=httpx.MockTransport(router)), router


def unreachable(exc_type) -> PrinterClient:
    def refuse(request):
        raise exc_type("no route to printer", request=request)
    return PrinterClient("http://printer:7125", transport=httpx.MockTransport(refuse))


async def test_status_queries_subscribed_objects():
    client, router = client_for({("GET", "/printer/objects/query"): ok({"status": IDLE_OBJECTS})})
    status = await client.status()
    assert status.idle and len(status.heads) == 4
    query = router.requests[0].url.query.decode()
    assert "print_stats=state,filename" in query and "exception_manager" in query


async def test_status_is_offline_when_unreachable():
    assert (await unreachable(httpx.ConnectError).status()).online is False


async def test_files_lists_only_gcode_newest_first():
    client, _ = client_for({("GET", "/server/files/list"): ok([
        {"path": "old.gcode", "size": 10, "modified": 1.0},
        {"path": "notes.txt", "size": 5, "modified": 3.0},
        {"path": "plates/new.gcode", "size": 20, "modified": 2.0},
    ])})
    assert [f.filename for f in await client.files()] == ["plates/new.gcode", "old.gcode"]


async def test_file_info_from_metadata_needs_no_tail():
    meta = {"size": 10, "modified": 5.0, "filament_colors": ["#FF0000"],
            "filament_type": "PLA", "referenced_tools": [0]}
    client, router = client_for({("GET", "/server/files/metadata"): ok(meta)})
    info = await client.file_info("cube.gcode")
    assert info.colours == (FileColour(0, "FF0000", "PLA"),)
    assert [r.url.path for r in router.requests] == ["/server/files/metadata"]


async def test_file_info_reads_gcode_tail_when_metadata_lacks_colours():  # Review Focus: names
    tail = (b"; filament used [mm] = 10.0, 0.0\n"
            b"; filament_colour = #00FF00;#0000FF\n"
            b"; filament_type = PETG;PLA\n")
    client, router = client_for({
        ("GET", "/server/files/metadata"): ok({"size": 10, "modified": 5.0}),
        ("GET", "/server/files/gcodes/plates/globe light.gcode"): httpx.Response(206, content=tail),
    })
    info = await client.file_info("plates/globe light.gcode")
    assert info.colours == (FileColour(0, "00FF00", "PETG"),)
    assert router.requests[0].url.params["filename"] == "plates/globe light.gcode"
    tail_request = router.requests[1]
    assert tail_request.url.raw_path == b"/server/files/gcodes/plates/globe%20light.gcode"
    assert tail_request.headers["range"] == "bytes=-131072"
    await client.file_info("plates/globe light.gcode")
    assert len(router.requests) == 3  # second call: metadata only, tail cached


async def test_file_info_missing_file_is_none():
    client, _ = client_for({})
    assert await client.file_info("gone.gcode") is None


async def test_upload_never_starts_the_print():
    seen = {}

    def upload(request):
        seen["body"] = request.content
        return httpx.Response(201, json={"item": {"path": "a.gcode"}, "print_started": False})

    client, _ = client_for({("POST", "/server/files/upload"): upload})
    await client.upload("a.gcode", b"G28\n")
    assert b'name="print"\r\n\r\nfalse' in seen["body"]
    assert b'name="root"\r\n\r\ngcodes' in seen["body"]


async def test_upload_that_started_a_print_is_an_error():
    client, _ = client_for({("POST", "/server/files/upload"):
                            httpx.Response(201, json={"print_started": True})})
    with pytest.raises(PrinterError, match="started"):
        await client.upload("a.gcode", b"G28\n")


async def test_apply_head_map_checks_readback():
    scripts = []

    def gcode(request):
        scripts.append(json.loads(request.content)["script"])
        return ok("ok")

    table = {"status": {"print_task_config": {"extruder_map_table": [2] + [0] * 31}}}
    client, _ = client_for({
        ("POST", "/printer/gcode/script"): gcode,
        ("GET", "/printer/objects/query"): ok(table),
    })
    await client.apply_head_map([(0, 2)])
    assert scripts == ["SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=2"]
    with pytest.raises(PrinterError, match="did not take effect"):
        await client.apply_head_map([(0, 1)])


async def test_start_print_sends_filename():
    client, router = client_for({("POST", "/printer/print/start"): ok("ok")})
    await client.start_print("plates/a b.gcode")
    assert router.requests[0].url.params["filename"] == "plates/a b.gcode"


async def test_server_error_page_becomes_printer_error():  # Review Focus: printer errors
    client, _ = client_for({("POST", "/printer/print/start"):
                            httpx.Response(500, text="<html>oops</html>")})
    with pytest.raises(PrinterError, match="HTTP 500"):
        await client.start_print("a.gcode")


async def test_unreachable_printer_raises_offline():
    with pytest.raises(PrinterOffline):
        await unreachable(httpx.ConnectTimeout).start_print("a.gcode")


async def test_history_parses_or_returns_none():
    jobs = {"count": 1, "jobs": [
        {"filename": "a.gcode", "start_time": 1760000000.0, "status": "completed"}]}
    client, router = client_for({("GET", "/server/history/list"): ok(jobs)})
    since = datetime(2025, 10, 9, tzinfo=timezone.utc)
    assert [j.filename for j in await client.history(since)] == ["a.gcode"]
    assert float(router.requests[0].url.params["since"]) == since.timestamp()
    missing, _ = client_for({})
    assert await missing.history(since) is None
    assert await unreachable(httpx.ConnectError).history(since) is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_printer_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.printer.client'`.

- [ ] **Step 4: Implement the client**

`app/printer/client.py`:

```python
"""HTTP client for the U1's Moonraker API."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from . import u1
from .models import FileEntry, FileInfo, HistoryJob, PrinterStatus

TAIL_BYTES = 131072
GCODE_SUFFIXES = (".gcode", ".gco", ".g")


class PrinterError(Exception):
    """The printer answered, but not with what was needed."""


class PrinterOffline(PrinterError):
    """The printer could not be reached."""


def _objects_query(objects: dict[str, list[str] | None]) -> str:
    return "&".join(name if not fields else f"{name}={','.join(fields)}"
                    for name, fields in objects.items())


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


class PrinterClient:
    def __init__(self, base_url: str, api_key: str | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        headers = {"X-Api-Key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout,
                                       transport=transport)
        self._info_cache: dict[tuple[str, float], FileInfo] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:  # connection refused, DNS, timeouts
            raise PrinterOffline(f"printer unreachable: {exc.__class__.__name__}") from exc

    def _check(self, method: str, path: str, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise PrinterError(
                f"{method} {path} failed: HTTP {response.status_code} {_error_text(response)}".strip())

    async def _result(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        self._check(method, path, response)
        try:
            return response.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PrinterError(f"{method} {path}: unexpected response") from exc

    async def query(self, objects: dict[str, list[str] | None]) -> dict[str, Any]:
        result = await self._result("GET", f"/printer/objects/query?{_objects_query(objects)}")
        return result.get("status", {})

    async def status(self) -> PrinterStatus:
        try:
            return u1.parse_status(await self.query(u1.SUBSCRIBE_OBJECTS))
        except PrinterOffline:
            return PrinterStatus.offline()
        except PrinterError:  # Moonraker up, Klipper not ready
            return PrinterStatus(online=True)

    async def files(self) -> list[FileEntry]:
        result = await self._result("GET", "/server/files/list", params={"root": "gcodes"})
        entries = []
        for item in result:
            name = item.get("path") or item.get("filename")
            if name and name.lower().endswith(GCODE_SUFFIXES):
                entries.append(FileEntry(name, int(item.get("size") or 0),
                                         float(item.get("modified") or 0)))
        return sorted(entries, key=lambda e: e.modified, reverse=True)

    async def file_info(self, filename: str) -> FileInfo | None:
        path = "/server/files/metadata"
        response = await self._request("GET", path, params={"filename": filename})
        if response.status_code == 404:
            return None
        self._check("GET", path, response)
        meta = response.json()["result"]
        key = (filename, float(meta.get("modified") or 0))
        if key not in self._info_cache:
            comments = u1.parse_gcode_comments(await self._tail(filename)) if u1.needs_gcode_tail(meta) else {}
            self._info_cache[key] = u1.parse_file_info(filename, meta, comments)
        return self._info_cache[key]

    async def _tail(self, filename: str) -> str:
        """Last TAIL_BYTES of the file; works whether or not the server honours Range."""
        path = f"/server/files/gcodes/{quote(filename)}"
        try:
            async with self._http.stream("GET", path, headers={"Range": f"bytes=-{TAIL_BYTES}"}) as response:
                if response.status_code >= 400:
                    return ""
                tail = bytearray()
                async for chunk in response.aiter_bytes():
                    tail.extend(chunk)
                    if len(tail) > 2 * TAIL_BYTES:
                        del tail[: len(tail) - TAIL_BYTES]
                return bytes(tail[-TAIL_BYTES:]).decode("utf-8", errors="replace")
        except httpx.TransportError as exc:
            raise PrinterOffline(f"printer unreachable: {exc.__class__.__name__}") from exc

    async def upload(self, filename: str, content: bytes) -> None:
        path = "/server/files/upload"
        response = await self._request(
            "POST", path,
            data={"root": "gcodes", "print": "false"},
            files={"file": (filename, content, "application/octet-stream")},
        )
        self._check("POST", path, response)
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body.get("result", body) if isinstance(body, dict) else {}
        if body.get("print_started"):
            raise PrinterError("the printer started the uploaded file; it should only store it")

    async def run_gcode(self, script: str) -> None:
        await self._result("POST", "/printer/gcode/script", json={"script": script})

    async def apply_head_map(self, assignments: Sequence[tuple[int, int]]) -> None:
        if not assignments:
            return
        await self.run_gcode(u1.head_map_script(assignments))
        objects = await self.query({"print_task_config": ["extruder_map_table"]})
        if not u1.head_map_matches(objects.get("print_task_config"), assignments):
            raise PrinterError("head mapping did not take effect")

    async def start_print(self, filename: str) -> None:
        await self._result("POST", "/printer/print/start", params={"filename": filename})

    async def history(self, since: datetime) -> list[HistoryJob] | None:
        """Jobs started after `since`, or None when history can't be read."""
        try:
            response = await self._request("GET", "/server/history/list", params={
                "since": since.timestamp(), "order": "asc", "limit": 100})
        except PrinterOffline:
            return None
        if response.status_code >= 400:
            return None
        try:
            return u1.parse_history(response.json()["result"])
        except (ValueError, KeyError, TypeError):
            return None

    async def thumbnail(self, path: str) -> bytes | None:
        response = await self._request("GET", f"/server/files/gcodes/{quote(path)}")
        return response.content if response.status_code == 200 else None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/printer/client.py tests/fakes.py tests/unit/test_printer_client.py
git commit -m "feat: Moonraker HTTP client for status, files, mapping and start"
```

---

### Task 5: Printer events and websocket watcher

**Files:**
- Create: `app/printer/events.py`, `app/printer/watcher.py`
- Modify: `tests/fakes.py` (add `FakeWS`, `connector`, `eventually`, `stop`)
- Test: `tests/unit/test_watcher.py`

**Interfaces:**
- Consumes: `u1.parse_status`, `u1.SUBSCRIBE_OBJECTS` (Task 3); `Clock` (Task 1).
- Produces:
  - `app.printer.events`: frozen dataclasses `Connected(at, status)`, `Disconnected(at)`, `Heartbeat(at)`, `PrintStarted(at, filename)`, `PrintFinished(at, filename, result)`; `Event` union; `EventBus` with `subscribe(handler)` and `async publish(event)` (handler errors are logged, not raised).
  - `app.printer.watcher`: `StateTracker` (`reset(status)`, `update(status, at) -> list[Event]`); `PrinterWatcher(ws_url, bus, clock, *, api_key=None, connect=None, heartbeat_seconds=30.0, max_backoff=30.0)` with attribute `status: PrinterStatus` and `async run()` (runs until cancelled).

- [ ] **Step 1: Add the websocket fakes**

Append to `tests/fakes.py`:

```python
import asyncio
import contextlib
import json


class FakeWS:
    """Stands in for a Moonraker websocket connection."""

    def __init__(self, objects: dict | None = None, *, reply: bool = True) -> None:
        self.objects = objects
        self.reply = reply
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "FakeWS":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        if self.reply and message.get("method") == "printer.objects.subscribe":
            await self.incoming.put({"jsonrpc": "2.0", "id": message["id"],
                                     "result": {"eventtime": 1.0, "status": self.objects}})

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def push(self, message: dict) -> None:
        self.incoming.put_nowait(message)

    def push_update(self, objects: dict) -> None:
        self.push({"jsonrpc": "2.0", "method": "notify_status_update", "params": [objects, 2.0]})

    def drop(self) -> None:
        self.incoming.put_nowait(ConnectionError("connection dropped"))


def connector(*sockets: FakeWS):
    """connect() replacement: hands out the given sockets, then ones that never answer."""
    queue = list(sockets)

    def connect(url: str) -> FakeWS:
        return queue.pop(0) if queue else FakeWS(reply=False)

    return connect


async def eventually(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_watcher.py`:

```python
import asyncio

from app.printer.events import (
    Connected, Disconnected, EventBus, Heartbeat, PrintFinished, PrintStarted,
)
from app.printer.models import PrintState
from app.printer.watcher import PrinterWatcher, StateTracker
from tests.factories import IDLE_OBJECTS, make_status, printing_status
from tests.fakes import FakeWS, connector, eventually, stop


def make_watcher(clock, *sockets, heartbeat=30.0):
    bus = EventBus()
    seen = []

    async def collect(event):
        seen.append(event)

    bus.subscribe(collect)
    watcher = PrinterWatcher("ws://printer/websocket", bus, clock,
                             connect=connector(*sockets), heartbeat_seconds=heartbeat)
    return watcher, seen


def test_tracker_detects_start_and_finish(clock):
    tracker = StateTracker()
    tracker.reset(make_status())
    now = clock.now()
    assert tracker.update(printing_status("a.gcode"), now) == [PrintStarted(at=now, filename="a.gcode")]
    done = make_status(state=PrintState.COMPLETE, filename="a.gcode")
    assert tracker.update(done, now) == [PrintFinished(at=now, filename="a.gcode", result="complete")]


def test_tracker_without_baseline_reports_nothing(clock):
    assert StateTracker().update(printing_status(), clock.now()) == []


async def test_connect_publishes_status(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    assert isinstance(seen[0], Connected) and seen[0].status.idle
    assert watcher.status.online
    assert ws.sent[0]["method"] == "printer.objects.subscribe"
    await stop(task)


async def test_print_start_and_finish_events(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    ws.push_update({"print_stats": {"state": "printing", "filename": "cube.gcode"}})
    ws.push_update({"print_stats": {"state": "paused"}})
    ws.push_update({"print_stats": {"state": "printing"}})
    ws.push_update({"print_stats": {"state": "complete"}})
    await eventually(lambda: any(isinstance(e, PrintFinished) for e in seen))
    starts = [e for e in seen if isinstance(e, PrintStarted)]
    finish = next(e for e in seen if isinstance(e, PrintFinished))
    assert [s.filename for s in starts] == ["cube.gcode"]  # resuming is not a new start
    assert (finish.filename, finish.result) == ("cube.gcode", "complete")
    await stop(task)


async def test_heartbeat_when_quiet(clock):
    watcher, seen = make_watcher(clock, FakeWS(IDLE_OBJECTS), heartbeat=0.02)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: any(isinstance(e, Heartbeat) for e in seen))
    await stop(task)


async def test_reconnects_after_drop(clock):
    first, second = FakeWS(IDLE_OBJECTS), FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, first, second)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    first.drop()
    await eventually(lambda: sum(isinstance(e, Connected) for e in seen) == 2)
    kinds = [type(e).__name__ for e in seen if not isinstance(e, Heartbeat)]
    assert kinds == ["Connected", "Disconnected", "Connected"]
    await stop(task)


async def test_klippy_shutdown_counts_as_disconnect(clock):
    ws = FakeWS(IDLE_OBJECTS)
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    ws.push({"jsonrpc": "2.0", "method": "notify_klippy_shutdown"})
    await eventually(lambda: any(isinstance(e, Disconnected) for e in seen))
    assert watcher.status.online is False
    await stop(task)


async def test_unrelated_messages_during_subscribe_are_skipped(clock):
    ws = FakeWS(IDLE_OBJECTS)
    ws.push({"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]})
    watcher, seen = make_watcher(clock, ws)
    task = asyncio.create_task(watcher.run())
    await eventually(lambda: seen)
    assert isinstance(seen[0], Connected)
    await stop(task)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_watcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.printer.events'`.

- [ ] **Step 4: Implement events and the watcher**

`app/printer/events.py`:

```python
"""Events published by the printer watcher."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from .models import PrinterStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Connected:
    at: datetime
    status: PrinterStatus


@dataclass(frozen=True)
class Disconnected:
    at: datetime


@dataclass(frozen=True)
class Heartbeat:
    at: datetime


@dataclass(frozen=True)
class PrintStarted:
    at: datetime
    filename: str | None


@dataclass(frozen=True)
class PrintFinished:
    at: datetime
    filename: str | None
    result: str


Event = Connected | Disconnected | Heartbeat | PrintStarted | PrintFinished
Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        for handler in list(self._handlers):
            try:
                await handler(event)
            except Exception:
                log.exception("event handler failed for %r", event)
```

`app/printer/watcher.py`:

```python
"""Keeps a websocket subscription to Moonraker and turns state changes into events."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import websockets

from app.clock import Clock

from . import u1
from .events import (
    Connected, Disconnected, Event, EventBus, Heartbeat, PrintFinished, PrintStarted,
)
from .models import PrinterStatus, PrintState

log = logging.getLogger(__name__)

ACTIVE = frozenset({PrintState.PRINTING, PrintState.PAUSED})
ENDED = frozenset({PrintState.COMPLETE, PrintState.CANCELLED, PrintState.ERROR, PrintState.STANDBY})
REPLY_TIMEOUT = 15.0


class WatcherError(Exception):
    pass


class StateTracker:
    """Detects print starts and ends between consecutive status snapshots."""

    def __init__(self) -> None:
        self._previous: PrinterStatus | None = None

    def reset(self, status: PrinterStatus) -> None:
        self._previous = status

    def update(self, status: PrinterStatus, at: datetime) -> list[Event]:
        previous, self._previous = self._previous, status
        if previous is None:
            return []
        if status.state is PrintState.PRINTING and previous.state not in ACTIVE:
            return [PrintStarted(at=at, filename=status.filename)]
        if previous.state in ACTIVE and status.state in ENDED:
            return [PrintFinished(at=at, filename=previous.filename, result=status.state.value)]
        return []


class PrinterWatcher:
    def __init__(self, ws_url: str, bus: EventBus, clock: Clock, *, api_key: str | None = None,
                 connect: Callable[[str], Any] | None = None, heartbeat_seconds: float = 30.0,
                 max_backoff: float = 30.0) -> None:
        headers = {"X-Api-Key": api_key} if api_key else None
        self._connect = connect or (lambda url: websockets.connect(
            url, max_size=None, open_timeout=10, additional_headers=headers))
        self._ws_url = ws_url
        self._bus = bus
        self._clock = clock
        self._heartbeat_seconds = heartbeat_seconds
        self._max_backoff = max_backoff
        self._objects: dict[str, dict[str, Any]] = {}
        self._tracker = StateTracker()
        self._next_id = 0
        self._connected = False
        self.status = PrinterStatus.offline()

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self._connect(self._ws_url) as ws:
                    await self._session(ws)
            except Exception as exc:  # network drops and protocol surprises alike: reconnect
                log.warning("printer connection lost: %s", exc)
            if self._connected:
                backoff = 1.0
            await self._mark_disconnected()
            await self._clock.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _call(self, ws: Any, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params,
                                  "id": request_id}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=REPLY_TIMEOUT))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise WatcherError(f"{method} failed: {message['error']}")
            return message.get("result") or {}

    async def _session(self, ws: Any) -> None:
        result = await self._call(ws, "printer.objects.subscribe", {"objects": u1.SUBSCRIBE_OBJECTS})
        self._objects = {name: dict(fields) for name, fields in (result.get("status") or {}).items()}
        self.status = u1.parse_status(self._objects)
        self._tracker.reset(self.status)
        self._connected = True
        last_beat = self._clock.now()
        await self._bus.publish(Connected(at=last_beat, status=self.status))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._heartbeat_seconds)
            except TimeoutError:
                raw = None
            now = self._clock.now()
            if raw is not None:
                await self._handle(json.loads(raw), now)
            if raw is None or (now - last_beat).total_seconds() >= self._heartbeat_seconds:
                await self._bus.publish(Heartbeat(at=now))
                last_beat = now

    async def _handle(self, message: dict, now: datetime) -> None:
        method = message.get("method")
        if method == "notify_status_update":
            params = message.get("params") or [{}]
            for name, fields in (params[0] or {}).items():
                self._objects.setdefault(name, {}).update(fields)
            self.status = u1.parse_status(self._objects)
            for event in self._tracker.update(self.status, now):
                await self._bus.publish(event)
        elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
            raise WatcherError(method)

    async def _mark_disconnected(self) -> None:
        self.status = PrinterStatus.offline()
        if self._connected:
            self._connected = False
            await self._bus.publish(Disconnected(at=self._clock.now()))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/printer/events.py app/printer/watcher.py tests/fakes.py tests/unit/test_watcher.py
git commit -m "feat: websocket watcher publishing connect, heartbeat and print events"
```

---

### Task 6: Watch-coverage gaps

**Files:**
- Create: `app/bedcheck/__init__.py`, `app/bedcheck/coverage.py`
- Test: `tests/unit/test_coverage.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Period(start: datetime, end: datetime)` (frozen dataclass) and `uncovered_gaps(periods: Iterable[Period], start, end, tolerance: timedelta) -> list[Period]`. It returns the parts of `[start, end]` outside every period, keeping only gaps strictly longer than `tolerance`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_coverage.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_coverage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bedcheck'`.

- [ ] **Step 3: Implement**

Create empty `app/bedcheck/__init__.py`, then `app/bedcheck/coverage.py`:

```python
"""Which parts of a time window were not watched (pure)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


def uncovered_gaps(periods: Iterable[Period], start: datetime, end: datetime,
                   tolerance: timedelta) -> list[Period]:
    """Parts of [start, end] outside every period that are longer than `tolerance`."""
    clipped = sorted(
        (max(p.start, start), min(p.end, end)) for p in periods if p.end > start and p.start < end
    )
    gaps: list[Period] = []
    cursor = start
    for begin, finish in clipped:
        if begin > cursor:
            gaps.append(Period(cursor, begin))
        cursor = max(cursor, finish)
    if cursor < end:
        gaps.append(Period(cursor, end))
    return [gap for gap in gaps if gap.end - gap.start > tolerance]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_coverage.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/bedcheck tests/unit/test_coverage.py
git commit -m "feat: find unwatched gaps in a time window"
```

---

### Task 7: Bed-confirmation service

**Files:**
- Create: `app/bedcheck/service.py`
- Modify: `tests/fakes.py` (add `FakeHistory`, `StatusHolder`)
- Test: `tests/unit/test_bedcheck.py`

**Interfaces:**
- Consumes: `uncovered_gaps`, `Period` (Task 6); events (Task 5); `to_db`/`from_db` (Task 2); `PrinterStatus`, `HistoryJob` (Task 3).
- Produces (all in `app.bedcheck.service`):
  - Reason constants `NO_CONFIRMATION = "no confirmation"`, `PRINTED = "printed since confirmation"`, `UNVERIFIED = "couldn't verify the printer stayed idle"`, `WITHDRAWN = "withdrawn"`, `REPLACED = "replaced"`, `CONSUMED = "consumed"`; `GAP_TOLERANCE = timedelta(minutes=2)`.
  - Frozen dataclasses `Confirmation(id, confirmed_at, ended_at, end_reason, consumed_by_job_id)`, `Valid(confirmation)`, `Invalid(reason)`; `Validity = Valid | Invalid`; `ConfirmRefused(Exception)`.
  - `not_idle_reason(status: PrinterStatus) -> str`.
  - `BedCheck(conn, clock, history: HistorySource, status: Callable[[], PrinterStatus], on_voided: Callable[[str], Awaitable[None]] | None = None)` with `set_void_listener(fn)`, `startup()`, `shutdown()`, `async handle_event(event)`, `confirm() -> Confirmation`, `withdraw()`, `consume(job_id)`, `latest()`, `active()`, `async validity(*, notify=True) -> Validity`.
  - `tests.fakes.FakeHistory(jobs=())` (`.jobs` list or `None`, `.calls`), `tests.fakes.StatusHolder(status)`.

- [ ] **Step 1: Add the fakes**

Append to `tests/fakes.py`:

```python
from dataclasses import dataclass

from app.printer.models import PrinterStatus


@dataclass
class StatusHolder:
    """Mutable printer status for tests: pass `lambda: holder.status` as the provider."""
    status: PrinterStatus


class FakeHistory:
    def __init__(self, jobs=()) -> None:
        self.jobs = list(jobs)   # set to None to simulate history being unavailable
        self.calls: list = []

    async def history(self, since):
        self.calls.append(since)
        return self.jobs
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_bedcheck.py`:

```python
from datetime import timedelta

import pytest

from app.bedcheck.service import (
    NO_CONFIRMATION, PRINTED, UNVERIFIED, WITHDRAWN, BedCheck, ConfirmRefused, Invalid, Valid,
)
from app.printer.events import Connected, Disconnected, Heartbeat, PrintStarted
from app.printer.models import HistoryJob, PrinterStatus
from tests.factories import make_status, printing_status
from tests.fakes import FakeHistory, StatusHolder


@pytest.fixture
def holder():
    return StatusHolder(make_status())


@pytest.fixture
def history():
    return FakeHistory()


@pytest.fixture
def voids():
    return []


@pytest.fixture
def bed(db, clock, history, holder, voids):
    async def on_voided(reason):
        voids.append(reason)

    check = BedCheck(db, clock, history, lambda: holder.status, on_voided=on_voided)
    check.startup()
    return check


async def connected(bed, clock, status=None):
    await bed.handle_event(Connected(at=clock.now(), status=status or make_status()))


async def gap(bed, clock, seconds):
    """Lose the printer for `seconds`, then reconnect."""
    await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(seconds)
    await connected(bed, clock)


async def test_confirm_needs_watched_idle_printer(bed, clock, holder):
    with pytest.raises(ConfirmRefused, match="not watching"):
        bed.confirm()
    await connected(bed, clock)
    holder.status = printing_status()
    with pytest.raises(ConfirmRefused, match="printer is printing"):
        bed.confirm()
    holder.status = PrinterStatus.offline()
    with pytest.raises(ConfirmRefused, match="printer is offline"):
        bed.confirm()


async def test_confirmation_stays_valid_while_watched(bed, clock):
    await connected(bed, clock)
    confirmation = bed.confirm()
    clock.advance(timedelta(hours=5))
    await bed.handle_event(Heartbeat(at=clock.now()))
    assert await bed.validity() == Valid(confirmation)


async def test_new_confirmation_replaces_old_quietly(bed, clock, db, voids):
    await connected(bed, clock)
    first = bed.confirm()
    second = bed.confirm()
    assert bed.active().id == second.id
    reason = db.execute("SELECT end_reason FROM confirmations WHERE id = ?", (first.id,)).fetchone()[0]
    assert reason == "replaced" and voids == []


async def test_print_start_voids_and_notifies(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(PrintStarted(at=clock.now(), filename="x.gcode"))
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]


async def test_withdraw_is_quiet(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    bed.withdraw()
    assert await bed.validity() == Invalid(WITHDRAWN) and voids == []


async def test_consumed_confirmation_reads_as_none(bed, clock):
    await connected(bed, clock)
    bed.confirm()
    bed.consume(job_id=7)
    assert await bed.validity() == Invalid(NO_CONFIRMATION)
    assert bed.latest().consumed_by_job_id == 7


async def test_short_disconnect_needs_no_history(bed, clock, history):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 60)
    assert isinstance(await bed.validity(), Valid) and history.calls == []


async def test_long_gap_is_checked_against_history(bed, clock, history):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    gap_start = clock.now()
    await gap(bed, clock, 600)
    assert isinstance(await bed.validity(), Valid)
    assert history.calls[0] == gap_start


async def test_print_found_in_history_voids_on_reconnect(bed, clock, history, voids, db):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    history.jobs = [HistoryJob("sneaky.gcode", clock.now() + timedelta(minutes=3), "completed")]
    await gap(bed, clock, 600)
    assert await bed.validity() == Invalid(PRINTED)
    assert voids == [PRINTED]
    assert db.execute("SELECT source FROM print_events").fetchone()[0] == "history"


async def test_history_unavailable_voids_as_unverified(bed, clock, history):
    history.jobs = None
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 600)
    assert await bed.validity() == Invalid(UNVERIFIED)


async def test_reconnect_while_printing_voids(bed, clock):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(Disconnected(at=clock.now()))
    await connected(bed, clock, status=printing_status())
    assert await bed.validity() == Invalid(PRINTED)


async def test_while_disconnected_it_is_unverified_but_not_voided(bed, clock, voids):
    await connected(bed, clock)
    bed.confirm()
    await bed.handle_event(Disconnected(at=clock.now()))
    clock.advance(600)
    assert await bed.validity() == Invalid(UNVERIFIED)
    assert bed.active() is not None and voids == []


async def test_app_restart_gap_is_checked(db, clock, history, holder):
    first = BedCheck(db, clock, history, lambda: holder.status)
    first.startup()
    await connected(first, clock)
    first.confirm()
    clock.advance(30)
    await first.handle_event(Heartbeat(at=clock.now()))
    clock.advance(600)  # process died without a Disconnected event
    history.jobs = None
    second = BedCheck(db, clock, history, lambda: holder.status)
    second.startup()
    await connected(second, clock)
    assert await second.validity() == Invalid(UNVERIFIED)


async def test_validity_without_notify_is_silent(bed, clock, history, voids):
    await connected(bed, clock)
    bed.confirm()
    clock.advance(60)
    await gap(bed, clock, 600)  # history [] → still valid
    history.jobs = None
    assert await bed.validity(notify=False) == Invalid(UNVERIFIED)
    assert voids == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_bedcheck.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bedcheck.service'`.

- [ ] **Step 4: Implement**

`app/bedcheck/service.py`:

```python
"""The bed-confirmation rule (spec §6): one confirmation, voided by any print."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.clock import Clock
from app.db import from_db, to_db
from app.printer.events import (
    Connected, Disconnected, Event, Heartbeat, PrintFinished, PrintStarted,
)
from app.printer.models import HistoryJob, PrinterStatus, PrintState

from .coverage import Period, uncovered_gaps

log = logging.getLogger(__name__)

GAP_TOLERANCE = timedelta(minutes=2)
NO_CONFIRMATION = "no confirmation"
PRINTED = "printed since confirmation"
UNVERIFIED = "couldn't verify the printer stayed idle"
WITHDRAWN = "withdrawn"
REPLACED = "replaced"
CONSUMED = "consumed"


@dataclass(frozen=True)
class Confirmation:
    id: int
    confirmed_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    consumed_by_job_id: int | None


@dataclass(frozen=True)
class Valid:
    confirmation: Confirmation


@dataclass(frozen=True)
class Invalid:
    reason: str


Validity = Valid | Invalid


class ConfirmRefused(Exception):
    pass


class HistorySource(Protocol):
    async def history(self, since: datetime) -> list[HistoryJob] | None: ...


VoidListener = Callable[[str], Awaitable[None]]


def not_idle_reason(status: PrinterStatus) -> str:
    if not status.online:
        return "printer is offline"
    if status.exception:
        return f"printer reports an error: {status.exception}"
    if status.paused or status.state is PrintState.PAUSED:
        return "printer is paused"
    if status.state is PrintState.PRINTING:
        return "printer is printing"
    if not status.klippy_ready:
        return "printer is not ready"
    return f"printer is not idle ({status.state.value})"


def _confirmation(row: sqlite3.Row) -> Confirmation:
    return Confirmation(
        id=row["id"],
        confirmed_at=from_db(row["confirmed_at"]),
        ended_at=from_db(row["ended_at"]),
        end_reason=row["end_reason"],
        consumed_by_job_id=row["consumed_by_job_id"],
    )


class BedCheck:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, history: HistorySource,
                 status: Callable[[], PrinterStatus], on_voided: VoidListener | None = None) -> None:
        self._conn = conn
        self._clock = clock
        self._history = history
        self._status = status
        self._on_voided = on_voided
        self._open_period: int | None = None

    def set_void_listener(self, listener: VoidListener) -> None:
        self._on_voided = listener

    # -- lifecycle -------------------------------------------------------------------

    def startup(self) -> None:
        """Close watch periods left open by a crash; they end at their last heartbeat."""
        self._conn.execute("UPDATE watch_periods SET closed = 1 WHERE closed = 0")

    def shutdown(self) -> None:
        if self._open_period is not None:
            self._close_period(self._clock.now())

    async def handle_event(self, event: Event) -> None:
        if isinstance(event, Connected):
            self._open_period = self._conn.execute(
                "INSERT INTO watch_periods (began_at, last_seen_at) VALUES (?, ?)",
                (to_db(event.at), to_db(event.at)),
            ).lastrowid
            if event.status.paused or event.status.state in (PrintState.PRINTING, PrintState.PAUSED):
                await self._void_active(PRINTED, notify=True)
            else:
                await self.validity(notify=True)  # settles any gap since the last period now
        elif isinstance(event, Heartbeat):
            self._touch(event.at)
        elif isinstance(event, PrintStarted):
            self._touch(event.at)
            self._record_print("started", event.at, event.filename, None, "live")
            await self._void_active(PRINTED, notify=True)
        elif isinstance(event, PrintFinished):
            self._touch(event.at)
            self._record_print("finished", event.at, event.filename, event.result, "live")
        elif isinstance(event, Disconnected) and self._open_period is not None:
            self._close_period(event.at)

    # -- confirmations ---------------------------------------------------------------

    def latest(self) -> Confirmation | None:
        row = self._conn.execute("SELECT * FROM confirmations ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _confirmation(row)

    def active(self) -> Confirmation | None:
        latest = self.latest()
        return latest if latest is not None and latest.ended_at is None else None

    def confirm(self) -> Confirmation:
        status = self._status()
        if not status.online or not status.idle:
            raise ConfirmRefused(not_idle_reason(status))
        if self._open_period is None:
            raise ConfirmRefused("not watching the printer yet; try again in a moment")
        now = self._clock.now()
        self._end_active(now, REPLACED)
        self._conn.execute("INSERT INTO confirmations (confirmed_at) VALUES (?)", (to_db(now),))
        return self.latest()

    def withdraw(self) -> None:
        self._end_active(self._clock.now(), WITHDRAWN)

    def consume(self, job_id: int) -> None:
        current = self.active()
        if current is None:
            raise ConfirmRefused("no active confirmation to use")
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ?, consumed_by_job_id = ? "
            "WHERE id = ?",
            (to_db(self._clock.now()), CONSUMED, job_id, current.id),
        )

    async def validity(self, *, notify: bool = True) -> Validity:
        current = self.active()
        if current is None:
            latest = self.latest()
            if latest is None or latest.end_reason in (CONSUMED, REPLACED, None):
                return Invalid(NO_CONFIRMATION)
            return Invalid(latest.end_reason)
        if self._open_period is None:
            return Invalid(UNVERIFIED)  # not watching right now; settled again on reconnect
        now = self._clock.now()
        gaps = uncovered_gaps(self._periods(current.confirmed_at, now), current.confirmed_at,
                              now, GAP_TOLERANCE)
        if not gaps:
            return Valid(current)
        jobs = await self._history.history(since=gaps[0].start)
        if jobs is None:
            reason = UNVERIFIED
        else:
            inside = [j for j in jobs if any(g.start <= j.start_time <= g.end for g in gaps)]
            if not inside:
                return Valid(current)
            for job in inside:
                self._record_print("started", job.start_time, job.filename, job.status, "history")
            reason = PRINTED
        await self._void(current.id, reason, notify)
        return Invalid(reason)

    # -- helpers ---------------------------------------------------------------------

    def _periods(self, since: datetime, now: datetime) -> list[Period]:
        rows = self._conn.execute(
            "SELECT id, began_at, last_seen_at FROM watch_periods WHERE last_seen_at >= ? OR id = ?",
            (to_db(since), self._open_period),
        ).fetchall()
        return [
            Period(from_db(r["began_at"]),
                   now if r["id"] == self._open_period else from_db(r["last_seen_at"]))
            for r in rows
        ]

    def _end_active(self, now: datetime, reason: str) -> None:
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ? WHERE ended_at IS NULL",
            (to_db(now), reason),
        )

    async def _void(self, confirmation_id: int, reason: str, notify: bool) -> None:
        self._conn.execute(
            "UPDATE confirmations SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
            (to_db(self._clock.now()), reason, confirmation_id),
        )
        if notify and self._on_voided is not None:
            try:
                await self._on_voided(reason)
            except Exception:
                log.exception("void listener failed")

    async def _void_active(self, reason: str, notify: bool) -> None:
        current = self.active()
        if current is not None:
            await self._void(current.id, reason, notify)

    def _touch(self, at: datetime) -> None:
        if self._open_period is not None:
            self._conn.execute("UPDATE watch_periods SET last_seen_at = ? WHERE id = ?",
                               (to_db(at), self._open_period))

    def _close_period(self, at: datetime) -> None:
        self._conn.execute("UPDATE watch_periods SET last_seen_at = ?, closed = 1 WHERE id = ?",
                           (to_db(at), self._open_period))
        self._open_period = None

    def _record_print(self, kind: str, at: datetime, filename: str | None, result: str | None,
                      source: str) -> None:
        self._conn.execute(
            "INSERT INTO print_events (at, kind, filename, result, source) VALUES (?, ?, ?, ?, ?)",
            (to_db(at), kind, filename, result, source),
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/bedcheck/service.py tests/fakes.py tests/unit/test_bedcheck.py
git commit -m "feat: bed confirmation rule with watch periods and history check"
```

---

### Task 8: Jobs model, store and local-time helpers

**Files:**
- Create: `app/timeutil.py`, `app/jobs/__init__.py`, `app/jobs/models.py`, `app/jobs/store.py`
- Modify: `tests/factories.py` (add `make_job`)
- Test: `tests/unit/test_timeutil.py`, `tests/unit/test_job_store.py`

**Interfaces:**
- Consumes: `to_db`/`from_db` (Task 2), `Clock` (Task 1).
- Produces:
  - `app.timeutil`: `LocalTimeError(ValueError)`, `parse_local_input(text) -> datetime` (naive, minute precision), `local_to_utc(naive, tz: ZoneInfo) -> datetime`, `format_local(moment, tz) -> str` (e.g. `"Thu 15 Oct 2026 02:00 (UTC+02:00)"`), `format_local_input(moment, tz) -> str` (e.g. `"2026-10-15T02:00"`). Error messages are full sentences: `"Pick a date and time."`, `"Not a valid date and time: '…'."`, `"02:30 on 2027-03-28 does not exist in Europe/Berlin (the clocks go forward)."`.
  - `app.jobs.models`: `JobStatus` (StrEnum pending/starting/started/skipped/failed/cancelled), `TERMINAL`, `HeadAssignment(logical, head, expected_type, expected_rgb)`, `Job(id, filename, file_size, file_modified, scheduled_at, head_map, status, outcome_reason, reminder_sent, created_at, decided_at)`.
  - `app.jobs.store`: `JobNotEditable(Exception)`; `JobStore(conn, clock)` with `create(*, filename, file_size, file_modified, scheduled_at, head_map) -> Job`, `get(id) -> Job | None`, `update(id, *, scheduled_at, head_map, file_size, file_modified) -> Job`, `cancel(id)`, `set_status(id, status, reason=None)`, `mark_reminder_sent(id)`, `pending()`, `due(now)`, `in_status(status)`, `finished(limit=50)`, `count_pending()`, `previous_pending(before, exclude_id=None) -> Job | None`.
  - `tests.factories.make_job(**overrides)` (filename `cube.gcode`, 2026-10-15 00:00 UTC, Head 3 red PETG).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_timeutil.py`:

```python
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
```

`tests/unit/test_job_store.py`:

```python
from datetime import timedelta

import pytest

from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.store import JobNotEditable, JobStore

HEADS = (HeadAssignment(logical=0, head=2, expected_type="PETG", expected_rgb="FF0000"),)


@pytest.fixture
def store(db, clock):
    return JobStore(db, clock)


def add(store, clock, minutes, filename="cube.gcode"):
    return store.create(filename=filename, file_size=1000, file_modified=1.5,
                        scheduled_at=clock.now() + timedelta(minutes=minutes), head_map=HEADS)


def test_create_and_read_back(store, clock):
    job = add(store, clock, 60)
    assert store.get(job.id) == job
    assert job.status is JobStatus.PENDING and job.head_map == HEADS
    assert job.scheduled_at == clock.now() + timedelta(minutes=60)


def test_pending_and_due_are_time_ordered(store, clock):
    add(store, clock, 60, "late.gcode")
    early = add(store, clock, 10, "early.gcode")
    assert [j.filename for j in store.pending()] == ["early.gcode", "late.gcode"]
    clock.advance(timedelta(minutes=30))
    assert [j.id for j in store.due(clock.now())] == [early.id]


def test_update_resets_reminder_and_only_for_pending(store, clock):  # Review Focus: finished job
    job = add(store, clock, 60)
    store.mark_reminder_sent(job.id)
    moved = store.update(job.id, scheduled_at=clock.now() + timedelta(hours=3), head_map=HEADS,
                         file_size=1000, file_modified=1.5)
    assert moved.reminder_sent is False
    store.set_status(job.id, JobStatus.SKIPPED, "no confirmation")
    with pytest.raises(JobNotEditable):
        store.update(job.id, scheduled_at=clock.now(), head_map=HEADS, file_size=1, file_modified=1.0)
    with pytest.raises(JobNotEditable):
        store.cancel(job.id)


def test_terminal_status_records_decision_time(store, clock):
    job = add(store, clock, 0)
    store.set_status(job.id, JobStatus.STARTING)
    assert store.get(job.id).decided_at is None
    store.set_status(job.id, JobStatus.FAILED, "interrupted")
    done = store.get(job.id)
    assert (done.status, done.outcome_reason, done.decided_at) == (
        JobStatus.FAILED, "interrupted", clock.now())
    assert store.in_status(JobStatus.FAILED) == [done]
    assert store.finished() == [done] and store.count_pending() == 0


def test_cancel_pending(store, clock):
    job = add(store, clock, 60)
    store.cancel(job.id)
    assert store.get(job.id).status is JobStatus.CANCELLED


def test_previous_pending(store, clock):
    a = add(store, clock, 60, "a.gcode")
    b = add(store, clock, 120, "b.gcode")
    assert store.previous_pending(clock.now() + timedelta(minutes=90)).id == a.id
    assert store.previous_pending(clock.now() + timedelta(minutes=130), exclude_id=b.id).id == a.id
    assert store.previous_pending(clock.now()) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_timeutil.py tests/unit/test_job_store.py -v`
Expected: FAIL with `ModuleNotFoundError` for `app.timeutil` and `app.jobs`.

- [ ] **Step 3: Implement the time helpers**

`app/timeutil.py`:

```python
"""Converting between the user's local time and stored UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class LocalTimeError(ValueError):
    """A local date and time the user typed can't be used."""


def parse_local_input(text: str) -> datetime:
    """Parse an <input type="datetime-local"> value into a naive local datetime."""
    text = (text or "").strip()
    if not text:
        raise LocalTimeError("Pick a date and time.")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise LocalTimeError(f"Not a valid date and time: {text!r}.") from None
    return value.replace(tzinfo=None, second=0, microsecond=0)


def local_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Reject local times skipped by a clock change; ambiguous times use the first occurrence."""
    utc = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        raise LocalTimeError(
            f"{naive:%H:%M} on {naive:%Y-%m-%d} does not exist in {tz.key} "
            "(the clocks go forward).")
    return utc


def format_local(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    offset = local.strftime("%z")
    return f"{local:%a %d %b %Y %H:%M} (UTC{offset[:3]}:{offset[3:]})"


def format_local_input(moment: datetime, tz: ZoneInfo) -> str:
    return f"{moment.astimezone(tz):%Y-%m-%dT%H:%M}"
```

- [ ] **Step 4: Implement the job model and store**

Create empty `app/jobs/__init__.py`.

`app/jobs/models.py`:

```python
"""Scheduled print jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    STARTED = "started"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.STARTED, JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.CANCELLED})


@dataclass(frozen=True)
class HeadAssignment:
    logical: int                  # colour number in the file
    head: int                     # 0-3
    expected_type: str | None     # material loaded in that head when scheduled
    expected_rgb: str | None      # colour loaded in that head when scheduled


@dataclass(frozen=True)
class Job:
    id: int
    filename: str
    file_size: int
    file_modified: float
    scheduled_at: datetime
    head_map: tuple[HeadAssignment, ...]
    status: JobStatus
    outcome_reason: str | None
    reminder_sent: bool
    created_at: datetime
    decided_at: datetime | None
```

`app/jobs/store.py`:

```python
"""SQLite storage for scheduled jobs."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime

from app.clock import Clock
from app.db import from_db, to_db

from .models import TERMINAL, HeadAssignment, Job, JobStatus


class JobNotEditable(Exception):
    """The job already ran, was skipped, failed or was cancelled."""


def _dump_map(head_map: Sequence[HeadAssignment]) -> str:
    return json.dumps([asdict(a) for a in head_map])


def _load_map(text: str) -> tuple[HeadAssignment, ...]:
    return tuple(HeadAssignment(**item) for item in json.loads(text))


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        filename=row["filename"],
        file_size=row["file_size"],
        file_modified=row["file_modified"],
        scheduled_at=from_db(row["scheduled_at_utc"]),
        head_map=_load_map(row["head_map_json"]),
        status=JobStatus(row["status"]),
        outcome_reason=row["outcome_reason"],
        reminder_sent=bool(row["reminder_sent"]),
        created_at=from_db(row["created_at"]),
        decided_at=from_db(row["decided_at"]),
    )


class JobStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def create(self, *, filename: str, file_size: int, file_modified: float,
               scheduled_at: datetime, head_map: Sequence[HeadAssignment]) -> Job:
        cursor = self._conn.execute(
            "INSERT INTO jobs (filename, file_size, file_modified, scheduled_at_utc, head_map_json,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (filename, file_size, file_modified, to_db(scheduled_at), _dump_map(head_map),
             to_db(self._clock.now())),
        )
        return self.get(cursor.lastrowid)

    def get(self, job_id: int) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job(row)

    def update(self, job_id: int, *, scheduled_at: datetime, head_map: Sequence[HeadAssignment],
               file_size: int, file_modified: float) -> Job:
        cursor = self._conn.execute(
            "UPDATE jobs SET scheduled_at_utc = ?, head_map_json = ?, file_size = ?,"
            " file_modified = ?, reminder_sent = 0 WHERE id = ? AND status = 'pending'",
            (to_db(scheduled_at), _dump_map(head_map), file_size, file_modified, job_id),
        )
        if cursor.rowcount == 0:
            raise JobNotEditable(job_id)
        return self.get(job_id)

    def cancel(self, job_id: int) -> None:
        cursor = self._conn.execute(
            "UPDATE jobs SET status = 'cancelled', decided_at = ? WHERE id = ? AND status = 'pending'",
            (to_db(self._clock.now()), job_id),
        )
        if cursor.rowcount == 0:
            raise JobNotEditable(job_id)

    def set_status(self, job_id: int, status: JobStatus, reason: str | None = None) -> None:
        decided = to_db(self._clock.now()) if status in TERMINAL else None
        self._conn.execute(
            "UPDATE jobs SET status = ?, outcome_reason = ?, decided_at = ? WHERE id = ?",
            (status.value, reason, decided, job_id),
        )

    def mark_reminder_sent(self, job_id: int) -> None:
        self._conn.execute("UPDATE jobs SET reminder_sent = 1 WHERE id = ?", (job_id,))

    def _select(self, where: str, params: tuple = (), order: str = "scheduled_at_utc, id",
                limit: int | None = None) -> list[Job]:
        sql = f"SELECT * FROM jobs WHERE {where} ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [_job(row) for row in self._conn.execute(sql, params).fetchall()]

    def pending(self) -> list[Job]:
        return self._select("status = 'pending'")

    def due(self, now: datetime) -> list[Job]:
        return self._select("status = 'pending' AND scheduled_at_utc <= ?", (to_db(now),))

    def in_status(self, status: JobStatus) -> list[Job]:
        return self._select("status = ?", (status.value,))

    def finished(self, limit: int = 50) -> list[Job]:
        return self._select("status != 'pending'",
                            order="COALESCE(decided_at, scheduled_at_utc) DESC, id DESC", limit=limit)

    def count_pending(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()[0]

    def previous_pending(self, before: datetime, exclude_id: int | None = None) -> Job | None:
        jobs = self._select("status = 'pending' AND scheduled_at_utc < ? AND id != ?",
                            (to_db(before), exclude_id if exclude_id is not None else -1),
                            order="scheduled_at_utc DESC, id DESC", limit=1)
        return jobs[0] if jobs else None
```

Add to `tests/factories.py`:

```python
from datetime import datetime, timezone

from app.jobs.models import HeadAssignment, Job, JobStatus


def make_job(**overrides) -> Job:
    when = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    values = dict(
        id=1, filename="cube.gcode", file_size=1000, file_modified=1_760_000_000.0,
        scheduled_at=when, head_map=(HeadAssignment(0, 2, "PETG", "FF0000"),),
        status=JobStatus.PENDING, outcome_reason=None, reminder_sent=False,
        created_at=when, decided_at=None,
    )
    values.update(overrides)
    return Job(**values)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/timeutil.py app/jobs tests/factories.py tests/unit/test_timeutil.py tests/unit/test_job_store.py
git commit -m "feat: job storage and local-time conversion with DST checks"
```

---

### Task 9: Head suggestion and assignment

**Files:**
- Create: `app/jobs/heads.py`
- Test: `tests/unit/test_heads.py`

**Interfaces:**
- Consumes: `FileColour`, `HeadFilament`, `HEAD_COUNT` (Task 3); `HeadAssignment` (Task 8).
- Produces: `describe_filament(rgb, material) -> str` (`"#FF0000 PLA"`, `"unknown colour, unknown material"`), `rgb_distance(a, b) -> float`, `suggest_heads(colours, heads) -> dict[int, int]` (logical → head), `HeadChoiceError(ValueError)`, `build_assignments(choice: Mapping[int, int], colours, heads) -> tuple[HeadAssignment, ...]` (lower-case error messages such as `"head 4 is empty"`).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_heads.py`:

```python
import pytest

from app.jobs.heads import (
    HeadChoiceError, build_assignments, describe_filament, suggest_heads,
)
from app.jobs.models import HeadAssignment
from app.printer.models import FileColour, HeadFilament
from tests.factories import DEFAULT_HEADS  # white PLA, black PLA, red PETG, empty


def test_prefers_same_material_then_nearest_colour():
    assert suggest_heads((FileColour(0, "FF1010", "PLA"),), DEFAULT_HEADS) == {0: 1}


def test_unknown_material_uses_nearest_colour():
    assert suggest_heads((FileColour(0, "FF1010", None),), DEFAULT_HEADS) == {0: 2}


def test_no_loaded_heads_falls_back_to_same_number():
    empty = tuple(HeadFilament(i, False) for i in range(4))
    colours = (FileColour(0, None, None), FileColour(1, None, None))
    assert suggest_heads(colours, empty) == {0: 0, 1: 1}


def test_build_assignments_copies_loaded_filament():
    colours = (FileColour(0, "FF0000", "PETG"),)
    assert build_assignments({0: 2}, colours, DEFAULT_HEADS) == (HeadAssignment(0, 2, "PETG", "FF0000"),)


def test_build_assignments_rejects_bad_choices():
    colours = (FileColour(0, None, None),)
    with pytest.raises(HeadChoiceError, match="head 4 is empty"):
        build_assignments({0: 3}, colours, DEFAULT_HEADS)
    with pytest.raises(HeadChoiceError, match="pick a head for colour 1"):
        build_assignments({}, colours, DEFAULT_HEADS)
    with pytest.raises(HeadChoiceError, match="head 9 does not exist"):
        build_assignments({0: 8}, colours, DEFAULT_HEADS)


def test_describe_filament():
    assert describe_filament("FF0000", "PLA") == "#FF0000 PLA"
    assert describe_filament(None, None) == "unknown colour, unknown material"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_heads.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.jobs.heads'`.

- [ ] **Step 3: Implement**

`app/jobs/heads.py`:

```python
"""Choosing heads for a file's colours, and describing filament in text."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from app.printer.models import HEAD_COUNT, FileColour, HeadFilament

from .models import HeadAssignment

NO_COLOUR_DISTANCE = 1000.0  # larger than any real RGB distance (max ≈ 442)


class HeadChoiceError(ValueError):
    pass


def describe_filament(rgb: str | None, material: str | None) -> str:
    material = material or "unknown material"
    return f"#{rgb} {material}" if rgb else f"unknown colour, {material}"


def rgb_distance(a: str | None, b: str | None) -> float:
    if not a or not b:
        return NO_COLOUR_DISTANCE
    return math.dist([int(a[i:i + 2], 16) for i in (0, 2, 4)],
                     [int(b[i:i + 2], 16) for i in (0, 2, 4)])


def _same_material(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def suggest_heads(colours: Sequence[FileColour], heads: Sequence[HeadFilament]) -> dict[int, int]:
    """Per colour: a loaded head with the same material, then the nearest colour."""
    loaded = [h for h in heads if h.present]
    suggestion: dict[int, int] = {}
    for colour in colours:
        if not loaded:
            suggestion[colour.logical] = colour.logical % HEAD_COUNT
            continue
        best = min(loaded, key=lambda h: (not _same_material(colour.material, h.material),
                                          rgb_distance(colour.rgb, h.rgb), h.head))
        suggestion[colour.logical] = best.head
    return suggestion


def build_assignments(choice: Mapping[int, int], colours: Sequence[FileColour],
                      heads: Sequence[HeadFilament]) -> tuple[HeadAssignment, ...]:
    """Turn the user's head choices into assignments that remember what was loaded."""
    by_head = {h.head: h for h in heads}
    assignments = []
    for colour in colours:
        if colour.logical not in choice:
            raise HeadChoiceError(f"pick a head for colour {colour.logical + 1}")
        head = by_head.get(choice[colour.logical])
        if head is None:
            raise HeadChoiceError(f"head {choice[colour.logical] + 1} does not exist")
        if not head.present:
            raise HeadChoiceError(f"head {head.head + 1} is empty")
        assignments.append(HeadAssignment(colour.logical, head.head, head.material, head.rgb))
    return tuple(assignments)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_heads.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/jobs/heads.py tests/unit/test_heads.py
git commit -m "feat: suggest and validate head choices per file colour"
```

---

### Task 10: Start gate

**Files:**
- Create: `app/jobs/gate.py`
- Test: `tests/unit/test_gate.py`

**Interfaces:**
- Consumes: `Valid`, `Invalid`, `Validity`, `Confirmation` (Task 7); `Job` (Task 8); `describe_filament` (Task 9); `PrinterStatus`, `FileInfo`, `PrintState` (Task 3).
- Produces: `GateSnapshot(job, status, validity, file)`, `Start()`, `Skip(reason)`, `Decision = Start | Skip`, `gate(snapshot) -> Decision`. Checks run in the order of spec §8 and return these reasons: `"printer offline"`, `"printer in error: <message>"`, `"printer not ready"`, `"printer in error: last print ended with an error"`, `"printer busy"`, the validity reason, `"file missing"`, `"file changed since scheduling"`, `"head <n>: expected <filament>, found <filament|nothing>"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_gate.py`:

```python
from datetime import datetime, timezone

import pytest

from app.bedcheck.service import Confirmation, Invalid, Valid
from app.jobs.gate import GateSnapshot, Skip, Start, gate
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.jobs.gate'`.

- [ ] **Step 3: Implement**

`app/jobs/gate.py`:

```python
"""Start-or-skip decision for a due job (spec §8). Pure: no I/O."""
from __future__ import annotations

from dataclasses import dataclass

from app.bedcheck.service import Invalid, Validity
from app.printer.models import FileInfo, PrinterStatus, PrintState

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


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


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
    heads = {h.head: h for h in status.heads}
    for assignment in job.head_map:
        head = heads.get(assignment.head)
        loaded = head is not None and head.present
        if not (loaded and _norm(head.material) == _norm(assignment.expected_type)
                and head.rgb == assignment.expected_rgb):
            found = describe_filament(head.rgb, head.material) if loaded else "nothing"
            expected = describe_filament(assignment.expected_rgb, assignment.expected_type)
            return Skip(f"head {assignment.head + 1}: expected {expected}, found {found}")
    return Start()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_gate.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/jobs/gate.py tests/unit/test_gate.py
git commit -m "feat: pure start gate for due jobs"
```

---

### Task 11: Notification texts and feed

**Files:**
- Create: `app/notify/__init__.py`, `app/notify/messages.py`, `app/notify/service.py`
- Test: `tests/unit/test_messages.py`, `tests/unit/test_notifier.py`

**Interfaces:**
- Consumes: `Job` (Task 8), `describe_filament` (Task 9), `format_local` (Task 8), `to_db`/`from_db` (Task 2).
- Produces:
  - `app.notify.messages`: `job_started(job)`, `job_skipped(job, reason, tz)`, `job_failed(job, reason)`, `print_finished(filename, result)`, `reminder(job, reason, tz)`, `confirmation_voided(reason, pending)`, all returning `str`.
  - `app.notify.service`: kind constants `JOB_STARTED`, `JOB_SKIPPED`, `JOB_FAILED`, `PRINT_FINISHED`, `REMINDER`, `CONFIRMATION_VOIDED`, `TEST`; `Notification` dataclass (`id, at, kind, text, job_id, read, telegram_status, telegram_attempts, telegram_error, telegram_next_at`); `Notifier(conn, clock, *, telegram_enabled)` with `add_listener(fn)`, `notify(kind, text, job_id=None)` (never raises), `latest(limit=5)`, `unread_count()`, `mark_all_read()`, `due_for_telegram(now)`, `telegram_sent(id)`, `telegram_failed(id, error, next_at: datetime | None)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_messages.py`:

```python
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
```

`tests/unit/test_notifier.py`:

```python
from datetime import timedelta

from app.notify.service import JOB_SKIPPED, Notifier


def test_notify_records_with_telegram_off(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=False)
    notifier.notify(JOB_SKIPPED, "Skipped", job_id=3)
    [row] = notifier.latest()
    assert (row.kind, row.text, row.job_id, row.read, row.telegram_status) == (
        JOB_SKIPPED, "Skipped", 3, False, "off")
    assert notifier.due_for_telegram(clock.now()) == []


def test_notify_queues_for_telegram_and_wakes_listener(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    woken = []
    notifier.add_listener(lambda: woken.append(1))
    notifier.notify("test", "hello")
    assert [n.text for n in notifier.due_for_telegram(clock.now())] == ["hello"]
    assert woken == [1]


def test_unread_and_mark_all_read(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=False)
    notifier.notify("a", "one")
    notifier.notify("b", "two")
    assert notifier.unread_count() == 2
    assert [n.text for n in notifier.latest()] == ["two", "one"]
    notifier.mark_all_read()
    assert notifier.unread_count() == 0


def test_notify_never_raises(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=False)
    db.close()
    notifier.notify("a", "still fine")  # logged, not raised


def test_failed_attempts_reschedule_then_give_up(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    notifier.notify("a", "x")
    [row] = notifier.due_for_telegram(clock.now())
    later = clock.now() + timedelta(seconds=10)
    notifier.telegram_failed(row.id, "HTTP 502", next_at=later)
    assert notifier.due_for_telegram(clock.now()) == []
    [again] = notifier.due_for_telegram(later)
    assert (again.telegram_attempts, again.telegram_error) == (1, "HTTP 502")
    notifier.telegram_failed(row.id, "HTTP 502", next_at=None)
    assert notifier.latest()[0].telegram_status == "failed"


def test_sent_rows_are_not_due_again(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    notifier.notify("a", "x")
    [row] = notifier.due_for_telegram(clock.now())
    notifier.telegram_sent(row.id)
    assert notifier.due_for_telegram(clock.now()) == []
    assert notifier.latest()[0].telegram_status == "sent"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_messages.py tests/unit/test_notifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.notify'`.

- [ ] **Step 3: Implement**

Create empty `app/notify/__init__.py`.

`app/notify/messages.py`:

```python
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
```

`app/notify/service.py`:

```python
"""Notification feed (SQLite) and Telegram delivery state."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock
from app.db import from_db, to_db

log = logging.getLogger(__name__)

JOB_STARTED = "job_started"
JOB_SKIPPED = "job_skipped"
JOB_FAILED = "job_failed"
PRINT_FINISHED = "print_finished"
REMINDER = "reminder"
CONFIRMATION_VOIDED = "confirmation_voided"
TEST = "test"


@dataclass(frozen=True)
class Notification:
    id: int
    at: datetime
    kind: str
    text: str
    job_id: int | None
    read: bool
    telegram_status: str
    telegram_attempts: int
    telegram_error: str | None
    telegram_next_at: datetime | None


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"], at=from_db(row["at"]), kind=row["kind"], text=row["text"],
        job_id=row["job_id"], read=bool(row["read"]), telegram_status=row["telegram_status"],
        telegram_attempts=row["telegram_attempts"], telegram_error=row["telegram_error"],
        telegram_next_at=from_db(row["telegram_next_at"]),
    )


class Notifier:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, *, telegram_enabled: bool) -> None:
        self._conn = conn
        self._clock = clock
        self._telegram_enabled = telegram_enabled
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def notify(self, kind: str, text: str, job_id: int | None = None) -> None:
        """Record a notification. Never raises: scheduling must not depend on it."""
        try:
            now = to_db(self._clock.now())
            queued = self._telegram_enabled
            self._conn.execute(
                "INSERT INTO notifications (at, kind, text, job_id, telegram_status, telegram_next_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, kind, text, job_id, "pending" if queued else "off", now if queued else None),
            )
        except Exception:
            log.exception("could not record %s notification", kind)
            return
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                log.exception("notification listener failed")

    def latest(self, limit: int = 5) -> list[Notification]:
        rows = self._conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
        return [_notification(row) for row in rows.fetchall()]

    def unread_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()[0]

    def mark_all_read(self) -> None:
        self._conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")

    def due_for_telegram(self, now: datetime) -> list[Notification]:
        rows = self._conn.execute(
            "SELECT * FROM notifications WHERE telegram_status = 'pending' AND telegram_next_at <= ?"
            " ORDER BY id", (to_db(now),))
        return [_notification(row) for row in rows.fetchall()]

    def telegram_sent(self, notification_id: int) -> None:
        self._conn.execute(
            "UPDATE notifications SET telegram_status = 'sent', telegram_attempts = telegram_attempts + 1,"
            " telegram_error = NULL, telegram_next_at = NULL WHERE id = ?", (notification_id,))

    def telegram_failed(self, notification_id: int, error: str, next_at: datetime | None) -> None:
        self._conn.execute(
            "UPDATE notifications SET telegram_status = ?, telegram_attempts = telegram_attempts + 1,"
            " telegram_error = ?, telegram_next_at = ? WHERE id = ?",
            ("failed" if next_at is None else "pending", error,
             None if next_at is None else to_db(next_at), notification_id),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/notify tests/unit/test_messages.py tests/unit/test_notifier.py
git commit -m "feat: notification texts and feed with Telegram queue state"
```

---

### Task 12: Telegram sender and delivery worker

**Files:**
- Create: `app/notify/telegram.py`
- Test: `tests/unit/test_telegram.py`

**Interfaces:**
- Consumes: `Notifier.due_for_telegram`, `telegram_sent`, `telegram_failed` (Task 11); `Clock` (Task 1).
- Produces: `RETRY_DELAYS = (10, 30, 60, 300, 900)`; `TelegramError(Exception)`; `redact(text, secret) -> str`; `TelegramSender(token, chat_id, *, transport=None, base_url="https://api.telegram.org", timeout=15.0)` with `async send(text)` and `async close()`; `DeliveryWorker(notifier, sender, clock, *, app_base_url=None, poll_seconds=5.0)` with `wake()`, `async deliver_due() -> int`, `async run()`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_telegram.py`:

```python
import json
from datetime import timedelta

import httpx
import pytest

from app.notify.service import Notifier
from app.notify.telegram import RETRY_DELAYS, DeliveryWorker, TelegramError, TelegramSender

TOKEN = "123456:SECRET-token"


def sender_with(handler) -> TelegramSender:
    return TelegramSender(TOKEN, "42", transport=httpx.MockTransport(handler))


class FlakySender:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        if self.failures:
            self.failures -= 1
            raise TelegramError("HTTP 502: <redacted>")
        self.sent.append(text)


async def test_sends_plain_text_to_the_chat():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    await sender_with(handler).send("hello")
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(seen[0].content) == {
        "chat_id": "42", "text": "hello", "disable_web_page_preview": True}


async def test_errors_never_contain_the_token():
    def refuse(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(TelegramError) as caught:
        await sender_with(refuse).send("hello")
    assert TOKEN not in str(caught.value) and "<redacted>" in str(caught.value)


async def test_api_error_is_reported():
    def reject(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramError, match="HTTP 400: Bad Request: chat not found"):
        await sender_with(reject).send("x")


async def test_worker_delivers_with_link(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    sender = FlakySender(0)
    worker = DeliveryWorker(notifier, sender, clock, app_base_url="http://pi.local:8000")
    notifier.notify("a", "Skipped cube.gcode")
    assert await worker.deliver_due() == 1
    assert sender.sent == ["Skipped cube.gcode\nhttp://pi.local:8000"]
    assert notifier.latest()[0].telegram_status == "sent"


async def test_worker_retry_schedule_then_failed(db, clock):
    notifier = Notifier(db, clock, telegram_enabled=True)
    worker = DeliveryWorker(notifier, FlakySender(100), clock)
    notifier.notify("a", "x")
    for delay in RETRY_DELAYS:
        await worker.deliver_due()
        row = notifier.latest()[0]
        assert row.telegram_status == "pending"
        assert row.telegram_next_at == clock.now() + timedelta(seconds=delay)
        clock.advance(delay)
    await worker.deliver_due()
    row = notifier.latest()[0]
    assert (row.telegram_status, row.telegram_attempts) == ("failed", 6)
    assert "<redacted>" in row.telegram_error


async def test_pending_rows_resume_after_restart(db, clock):
    Notifier(db, clock, telegram_enabled=True).notify("a", "queued before restart")
    sender = FlakySender(0)
    await DeliveryWorker(Notifier(db, clock, telegram_enabled=True), sender, clock).deliver_due()
    assert sender.sent == ["queued before restart"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_telegram.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.notify.telegram'`.

- [ ] **Step 3: Implement**

`app/notify/telegram.py`:

```python
"""Send-only Telegram delivery with retries. The bot token never leaves this module."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol

import httpx

from app.clock import Clock

from .service import Notifier

log = logging.getLogger(__name__)

RETRY_DELAYS = (10, 30, 60, 300, 900)  # seconds before retries 1-5; then the row is failed


class TelegramError(Exception):
    """Delivery failed. The message never contains the bot token."""


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "<redacted>") if secret else text


class Sender(Protocol):
    async def send(self, text: str) -> None: ...


class TelegramSender:
    def __init__(self, token: str, chat_id: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str = "https://api.telegram.org", timeout: float = 15.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> None:
        try:
            response = await self._http.post(
                f"/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            )
        except httpx.HTTPError as exc:
            # `from None`: the original exception's text includes the URL, and so the token.
            raise TelegramError(redact(f"{exc.__class__.__name__}: {exc}", self._token)) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            detail = body.get("description") or response.text[:200]
            raise TelegramError(redact(f"HTTP {response.status_code}: {detail}", self._token))


class DeliveryWorker:
    def __init__(self, notifier: Notifier, sender: Sender, clock: Clock, *,
                 app_base_url: str | None = None, poll_seconds: float = 5.0) -> None:
        self._notifier = notifier
        self._sender = sender
        self._clock = clock
        self._base_url = app_base_url
        self._poll_seconds = poll_seconds
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def deliver_due(self) -> int:
        sent = 0
        for item in self._notifier.due_for_telegram(self._clock.now()):
            text = f"{item.text}\n{self._base_url}" if self._base_url else item.text
            try:
                await self._sender.send(text)
            except TelegramError as exc:
                attempt = item.telegram_attempts + 1
                next_at = (self._clock.now() + timedelta(seconds=RETRY_DELAYS[attempt - 1])
                           if attempt <= len(RETRY_DELAYS) else None)
                self._notifier.telegram_failed(item.id, str(exc), next_at)
                log.warning("Telegram delivery failed (attempt %d): %s", attempt, exc)
            else:
                self._notifier.telegram_sent(item.id)
                sent += 1
        return sent

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.deliver_due()
            except Exception:
                log.exception("Telegram delivery loop failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/notify/telegram.py tests/unit/test_telegram.py
git commit -m "feat: send-only Telegram delivery with retries and token redaction"
```

---

### Task 13: Job runner

**Files:**
- Create: `app/jobs/runner.py`
- Modify: `tests/fakes.py` (add `FakePrinter`)
- Test: `tests/unit/test_runner.py`

**Interfaces:**
- Consumes: `JobStore` (Task 8), `BedCheck`, `Valid` (Task 7), `gate`, `GateSnapshot`, `Skip` (Task 10), `Notifier` and kinds (Task 11), `messages` (Task 11), `PrinterError`, `PrinterOffline` (Task 4).
- Produces: `RunnerSettings(tz, dry_run=False, offline_grace=timedelta(minutes=10), start_verify_seconds=90, reminder=timedelta(minutes=60), tick_seconds=30.0)`; `Runner(*, jobs, bedcheck, printer, status, notifier, clock, settings)` with `wake()`, `async startup()`, `async tick()`, `async run()`. `tests.fakes.FakePrinter(files=None)` with `files_by_name`, `calls`, `fail_on`, `on_start`, `uploaded`, `history_jobs` and async `files`, `file_info`, `upload`, `apply_head_map`, `start_print`, `history`, `thumbnail`.

- [ ] **Step 1: Add the fake printer**

Append to `tests/fakes.py`:

```python
from app.printer.models import FileEntry, FileInfo


class FakePrinter:
    """In-memory printer offering the PrinterClient methods the app uses."""

    def __init__(self, files: dict[str, FileInfo] | None = None) -> None:
        self.files_by_name: dict[str, FileInfo] = dict(files or {})
        self.calls: list[tuple] = []
        self.fail_on: dict[str, Exception] = {}
        self.on_start: Callable[[str], None] | None = None
        self.uploaded: dict[str, bytes] = {}
        self.history_jobs: list | None = []

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise self.fail_on[method]

    async def files(self) -> list[FileEntry]:
        self._maybe_fail("files")
        return [FileEntry(f.filename, f.size, f.modified) for f in self.files_by_name.values()]

    async def file_info(self, filename: str) -> FileInfo | None:
        self.calls.append(("file_info", filename))
        self._maybe_fail("file_info")
        return self.files_by_name.get(filename)

    async def upload(self, filename: str, content: bytes) -> None:
        self._maybe_fail("upload")
        self.uploaded[filename] = content
        self.files_by_name[filename] = FileInfo(filename, len(content), 1.0)

    async def apply_head_map(self, assignments) -> None:
        self.calls.append(("apply_head_map", list(assignments)))
        self._maybe_fail("apply_head_map")

    async def start_print(self, filename: str) -> None:
        self.calls.append(("start_print", filename))
        self._maybe_fail("start_print")
        if self.on_start is not None:
            self.on_start(filename)

    async def history(self, since):
        return self.history_jobs

    async def thumbnail(self, path: str) -> bytes | None:
        self._maybe_fail("thumbnail")
        return b"PNG"
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_runner.py`:

```python
from dataclasses import dataclass, replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bedcheck.service import NO_CONFIRMATION, BedCheck, Invalid, Valid
from app.clock import FakeClock
from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.runner import Runner, RunnerSettings
from app.jobs.store import JobStore
from app.notify.service import JOB_SKIPPED, JOB_STARTED, REMINDER, Notifier
from app.printer.client import PrinterError
from app.printer.events import Connected
from app.printer.models import PrinterStatus
from tests.factories import make_file, make_status, printing_status
from tests.fakes import FakePrinter, StatusHolder

SETTINGS = RunnerSettings(tz=ZoneInfo("UTC"), start_verify_seconds=5)


@dataclass
class Env:
    clock: FakeClock
    jobs: JobStore
    bed: BedCheck
    printer: FakePrinter
    holder: StatusHolder
    notifier: Notifier
    runner: Runner

    def runner_with(self, **overrides) -> Runner:
        return Runner(jobs=self.jobs, bedcheck=self.bed, printer=self.printer,
                      status=lambda: self.holder.status, notifier=self.notifier, clock=self.clock,
                      settings=replace(SETTINGS, **overrides))

    def schedule(self, minutes: float = 0, filename: str = "cube.gcode"):
        info = self.printer.files_by_name[filename]
        return self.jobs.create(
            filename=filename, file_size=info.size, file_modified=info.modified,
            scheduled_at=self.clock.now() + timedelta(minutes=minutes),
            head_map=(HeadAssignment(0, 0, "PLA", "FFFFFF"),))

    def kinds(self) -> list[str]:
        return [n.kind for n in reversed(self.notifier.latest(50))]


@pytest.fixture
async def env(db, clock) -> Env:
    holder = StatusHolder(make_status())
    printer = FakePrinter(files={"cube.gcode": make_file()})
    printer.on_start = lambda filename: setattr(holder, "status", printing_status(filename))
    bed = BedCheck(db, clock, printer, lambda: holder.status)
    bed.startup()
    await bed.handle_event(Connected(at=clock.now(), status=holder.status))
    jobs = JobStore(db, clock)
    notifier = Notifier(db, clock, telegram_enabled=False)
    environment = Env(clock, jobs, bed, printer, holder, notifier, runner=None)
    environment.runner = environment.runner_with()
    return environment


async def test_happy_path_starts_and_consumes(env):
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.STARTED
    assert env.printer.calls == [("file_info", "cube.gcode"), ("apply_head_map", [(0, 0)]),
                                 ("start_print", "cube.gcode")]
    assert await env.bed.validity() == Invalid(NO_CONFIRMATION)
    assert env.kinds() == [JOB_STARTED]


async def test_no_confirmation_skips(env):
    job = env.schedule()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.SKIPPED, "no confirmation")
    assert ("start_print", "cube.gcode") not in env.printer.calls
    assert env.kinds() == [JOB_SKIPPED]


async def test_offline_retries_within_grace_then_skips(env):
    env.bed.confirm()
    job = env.schedule()
    env.holder.status = PrinterStatus.offline()
    await env.runner.tick()
    assert env.jobs.get(job.id).status is JobStatus.PENDING
    env.clock.advance(timedelta(minutes=11))
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "printer offline"


async def test_dry_run_changes_nothing(env):
    runner = env.runner_with(dry_run=True)
    env.bed.confirm()
    job = env.schedule()
    await runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "dry run (all checks passed)"
    assert [call[0] for call in env.printer.calls] == ["file_info"]
    assert isinstance(await env.bed.validity(), Valid)


async def test_start_error_fails_and_uses_up_confirmation(env):
    env.printer.fail_on["start_print"] = PrinterError("HTTP 500 boom")
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    done = env.jobs.get(job.id)
    assert (done.status, done.outcome_reason) == (JobStatus.FAILED, "HTTP 500 boom")
    assert await env.bed.validity() == Invalid(NO_CONFIRMATION)


async def test_printer_that_never_starts_fails(env):
    env.printer.on_start = None
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "printer did not start"


async def test_unreadable_file_skips_with_reason(env):  # Review Focus: printer errors
    env.printer.fail_on["file_info"] = PrinterError("GET /server/files/metadata failed: HTTP 500")
    env.bed.confirm()
    job = env.schedule()
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == (
        "could not read file: GET /server/files/metadata failed: HTTP 500")


async def test_changed_file_skips(env):
    env.bed.confirm()
    job = env.schedule()
    env.printer.files_by_name["cube.gcode"] = make_file(size=5)
    await env.runner.tick()
    assert env.jobs.get(job.id).outcome_reason == "file changed since scheduling"


async def test_second_job_at_the_same_time_is_skipped(env):
    env.bed.confirm()
    first, second = env.schedule(), env.schedule()
    await env.runner.tick()
    assert env.jobs.get(first.id).status is JobStatus.STARTED
    assert env.jobs.get(second.id).outcome_reason == "printer busy"


async def test_startup_recovers_interrupted_and_missed_jobs(env):
    interrupted = env.schedule(-1)
    env.jobs.set_status(interrupted.id, JobStatus.STARTING)
    missed, recent = env.schedule(-20), env.schedule(-5)
    await env.runner.startup()
    assert env.jobs.get(interrupted.id).outcome_reason == "interrupted"
    assert env.jobs.get(missed.id).outcome_reason == "missed (app not running)"
    assert env.jobs.get(recent.id).status is JobStatus.PENDING


async def test_reminder_sent_once_when_unconfirmed(env):
    job = env.schedule(30)
    await env.runner.tick()
    await env.runner.tick()
    assert env.kinds() == [REMINDER]
    assert env.jobs.get(job.id).reminder_sent


async def test_no_reminder_when_confirmed_or_far_away(env):
    env.bed.confirm()
    env.schedule(30)
    env.schedule(120)
    await env.runner.tick()
    assert env.kinds() == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.jobs.runner'`.

- [ ] **Step 4: Implement**

`app/jobs/runner.py`:

```python
"""Runs due jobs: offline retries, the gate, the start sequence, reminders and recovery."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.bedcheck.service import BedCheck, Valid
from app.clock import Clock
from app.notify import messages
from app.notify.service import JOB_FAILED, JOB_SKIPPED, JOB_STARTED, REMINDER, Notifier
from app.printer.client import PrinterError, PrinterOffline
from app.printer.models import FileInfo, PrinterStatus, PrintState

from .gate import GateSnapshot, Skip, gate
from .models import Job, JobStatus
from .store import JobStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerSettings:
    tz: ZoneInfo
    dry_run: bool = False
    offline_grace: timedelta = timedelta(minutes=10)
    start_verify_seconds: int = 90
    reminder: timedelta = timedelta(minutes=60)
    tick_seconds: float = 30.0


class PrinterPort(Protocol):
    async def file_info(self, filename: str) -> FileInfo | None: ...

    async def apply_head_map(self, assignments: Sequence[tuple[int, int]]) -> None: ...

    async def start_print(self, filename: str) -> None: ...


class Runner:
    def __init__(self, *, jobs: JobStore, bedcheck: BedCheck, printer: PrinterPort,
                 status: Callable[[], PrinterStatus], notifier: Notifier, clock: Clock,
                 settings: RunnerSettings) -> None:
        self._jobs = jobs
        self._bedcheck = bedcheck
        self._printer = printer
        self._status = status
        self._notifier = notifier
        self._clock = clock
        self._settings = settings
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        await self.startup()
        while True:
            self._wake.clear()
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._sleep_seconds())
            except TimeoutError:
                pass

    async def startup(self) -> None:
        for job in self._jobs.in_status(JobStatus.STARTING):
            self._finish(job, JobStatus.FAILED, "interrupted")
        now = self._clock.now()
        for job in self._jobs.pending():
            if now - job.scheduled_at > self._settings.offline_grace:
                self._finish(job, JobStatus.SKIPPED, "missed (app not running)")

    async def tick(self) -> None:
        now = self._clock.now()
        await self._remind(now)
        for job in self._jobs.due(now):
            await self._run_job(job)

    def _sleep_seconds(self) -> float:
        now = self._clock.now()
        moments: list[datetime] = []
        for job in self._jobs.pending():
            moments.append(job.scheduled_at)
            if not job.reminder_sent:
                moments.append(job.scheduled_at - self._settings.reminder)
        upcoming = [(m - now).total_seconds() for m in moments if m > now]
        return max(0.5, min([self._settings.tick_seconds, *upcoming]))

    async def _remind(self, now: datetime) -> None:
        for job in self._jobs.pending():
            in_window = job.scheduled_at - self._settings.reminder <= now < job.scheduled_at
            if job.reminder_sent or not in_window:
                continue
            validity = await self._bedcheck.validity(notify=False)
            if isinstance(validity, Valid):
                continue
            self._notifier.notify(REMINDER, messages.reminder(job, validity.reason, self._settings.tz),
                                  job.id)
            self._jobs.mark_reminder_sent(job.id)

    async def _run_job(self, job: Job) -> None:
        within_grace = self._clock.now() - job.scheduled_at <= self._settings.offline_grace
        if not self._status().online:
            if not within_grace:
                self._finish(job, JobStatus.SKIPPED, "printer offline")
            return  # otherwise retry on a later tick
        try:
            file = await self._printer.file_info(job.filename)
        except PrinterOffline:
            if not within_grace:
                self._finish(job, JobStatus.SKIPPED, "printer offline")
            return
        except PrinterError as exc:
            self._finish(job, JobStatus.SKIPPED, f"could not read file: {exc}")
            return
        validity = await self._bedcheck.validity(notify=False)
        decision = gate(GateSnapshot(job=job, status=self._status(), validity=validity, file=file))
        if isinstance(decision, Skip):
            self._finish(job, JobStatus.SKIPPED, decision.reason)
            return
        if self._settings.dry_run:
            log.info("DRY RUN: would map heads %s and start %s",
                     [(a.logical, a.head) for a in job.head_map], job.filename)
            self._finish(job, JobStatus.SKIPPED, "dry run (all checks passed)")
            return
        self._jobs.set_status(job.id, JobStatus.STARTING)
        try:
            self._bedcheck.consume(job.id)
            await self._printer.apply_head_map([(a.logical, a.head) for a in job.head_map])
            await self._printer.start_print(job.filename)
        except Exception as exc:
            self._finish(job, JobStatus.FAILED, str(exc) or exc.__class__.__name__)
            return
        if await self._verify_started(job.filename):
            self._jobs.set_status(job.id, JobStatus.STARTED)
            self._notifier.notify(JOB_STARTED, messages.job_started(job), job.id)
        else:
            self._finish(job, JobStatus.FAILED, "printer did not start")

    async def _verify_started(self, filename: str) -> bool:
        waited = 0.0
        while True:
            status = self._status()
            if status.state in (PrintState.PRINTING, PrintState.PAUSED) and status.filename == filename:
                return True
            if waited >= self._settings.start_verify_seconds:
                return False
            await self._clock.sleep(1.0)
            waited += 1.0

    def _finish(self, job: Job, status: JobStatus, reason: str) -> None:
        self._jobs.set_status(job.id, status, reason)
        if status is JobStatus.SKIPPED:
            text = messages.job_skipped(job, reason, self._settings.tz)
            self._notifier.notify(JOB_SKIPPED, text, job.id)
        else:
            self._notifier.notify(JOB_FAILED, messages.job_failed(job, reason), job.id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/jobs/runner.py tests/fakes.py tests/unit/test_runner.py
git commit -m "feat: job runner with gate, start verification, reminders and recovery"
```

---

### Task 14: Web foundation and login

**Files:**
- Create: `app/web/__init__.py`, `app/web/deps.py`, `app/web/app.py`, `app/web/auth.py`
- Create: `app/web/templates/base.html`, `app/web/templates/login.html`, `app/web/templates/message.html`, `app/web/static/app.css`, `app/web/static/htmx.min.js` (downloaded)
- Create: `tests/web/__init__.py`, `tests/web/conftest.py`
- Test: `tests/web/test_auth.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `BedCheck` (Task 7), `JobStore` (Task 8), `Notifier` (Task 11), `format_local` (Task 8), fakes from Tasks 7 and 13.
- Produces:
  - `app.web.deps`: `Services(settings, clock, status, printer, bedcheck, jobs, notifier, wake_runner)` dataclass with `tz` property; `services(request) -> Services`; `render(request, name, context=None, status_code=200)`.
  - `app.web.app.create_app(svc: Services, *, secret_key: str, lifespan=None) -> FastAPI`. Templates get `temp(t)` (global), `|local` and `|duration` (filters), plus `logged_in` and `unread` in every page.
  - Routes `GET/POST /login`, `POST /logout`, `GET /healthz` (public: `{"ok": true, "printer_online": bool}`).
  - Test fixtures `make_web(**settings_overrides) -> Web` and `web`. `Web` has `client, clock, holder, printer, bedcheck, jobs, notifier, woken` and the methods `login()`, `watch_printer()` and `event(e)`.

- [ ] **Step 1: Vendor htmx and write static files and templates**

Run: `mkdir -p app/web/static app/web/templates && curl -sSfL -o app/web/static/htmx.min.js https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js && head -c 60 app/web/static/htmx.min.js`
Expected: the start of minified JavaScript (e.g. `var htmx=function(){`).

`app/web/static/app.css`:

```css
:root { --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --card: #fff; --line: #e7e5e4;
        --accent: #2563eb; --bad: #b91c1c; --warn: #a16207; --good: #15803d; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --card: #292524; --line: #44403c;
          --accent: #60a5fa; --bad: #f87171; --warn: #facc15; --good: #4ade80; }
}
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.5 system-ui, sans-serif; background: var(--bg); color: var(--fg); }
header nav { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; padding: .75rem 1rem;
             border-bottom: 1px solid var(--line); }
main { max-width: 52rem; margin: 0 auto; padding: 1rem; }
section, fieldset { background: var(--card); border: 1px solid var(--line); border-radius: .5rem;
                    padding: 1rem; margin: 0 0 1rem; }
a { color: var(--accent); }
button { font: inherit; padding: .5rem 1rem; border-radius: .375rem; border: 1px solid var(--line);
         background: var(--accent); color: #fff; cursor: pointer; }
button.link { background: none; color: var(--accent); border: none; padding: 0; }
button:disabled { opacity: .5; cursor: not-allowed; }
form.inline { display: inline; }
input, select { font: inherit; padding: .4rem; max-width: 100%; }
table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: .4rem; border-bottom: 1px solid var(--line); vertical-align: top; }
.swatch { display: inline-block; width: 1em; height: 1em; border-radius: 50%;
          border: 1px solid var(--line); vertical-align: -.15em; }
.heads { list-style: none; padding: 0; }
.badge { background: var(--bad); color: #fff; border-radius: 1em; padding: 0 .5em; font-size: .8em; }
.error, .offline { color: var(--bad); }
.warning { color: var(--warn); }
.ok { color: var(--good); }
.hint { color: var(--muted); font-size: .9em; }
img.thumb { width: 64px; height: 64px; object-fit: contain; }
@media (max-width: 40rem) {
  table, tbody, tr, td { display: block; }
  th { display: none; }
  td { border: none; padding: .2rem 0; }
  tr { border-bottom: 1px solid var(--line); padding: .5rem 0; }
}
```

`app/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Schedule Print{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body>
  {% if logged_in %}
  <header>
    <nav>
      <a href="/">Printer</a>
      <a href="/files">Files</a>
      <a href="/jobs">Jobs</a>
      <a href="/notifications">Notifications{% if unread %} <span class="badge">{{ unread }}</span>{% endif %}</a>
      <form method="post" action="/logout" class="inline"><button class="link">Log out</button></form>
    </nav>
  </header>
  {% endif %}
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`app/web/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Log in · Schedule Print{% endblock %}
{% block content %}
<section>
  <h1>Schedule Print</h1>
  <form method="post" action="/login">
    <label>Password <input type="password" name="password" autofocus required></label>
    <button>Log in</button>
  </form>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
</section>
{% endblock %}
```

`app/web/templates/message.html`:

```html
{% extends "base.html" %}
{% block content %}
<section>
  <p class="error">{{ message }}</p>
  <p><a href="/jobs">Back to jobs</a></p>
</section>
{% endblock %}
```

- [ ] **Step 2: Write the test fixtures and failing tests**

Create empty `tests/web/__init__.py`.

`tests/web/conftest.py`:

```python
import asyncio
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.bedcheck.service import BedCheck
from app.clock import FakeClock
from app.config import Settings
from app.jobs.store import JobStore
from app.notify.service import Notifier
from app.printer.events import Connected
from app.web.app import create_app
from app.web.deps import Services
from tests.factories import make_file, make_status
from tests.fakes import FakePrinter, StatusHolder


@dataclass
class Web:
    client: TestClient
    clock: FakeClock
    holder: StatusHolder
    printer: FakePrinter
    bedcheck: BedCheck
    jobs: JobStore
    notifier: Notifier
    woken: list

    def login(self) -> None:
        response = self.client.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert response.status_code == 303

    def event(self, event) -> None:
        asyncio.run(self.bedcheck.handle_event(event))

    def watch_printer(self) -> None:
        self.event(Connected(at=self.clock.now(), status=self.holder.status))


@pytest.fixture
def make_web(db, clock, tmp_path):
    def build(**settings_overrides) -> Web:
        holder = StatusHolder(make_status())
        printer = FakePrinter(files={"cube.gcode": make_file()})
        bedcheck = BedCheck(db, clock, printer, lambda: holder.status)
        bedcheck.startup()
        jobs = JobStore(db, clock)
        notifier = Notifier(db, clock, telegram_enabled=False)
        woken: list = []
        settings = Settings(printer_host="sim", app_password="secret", tz="Europe/Berlin",
                            data_dir=tmp_path, **settings_overrides)
        svc = Services(settings=settings, clock=clock, status=lambda: holder.status,
                       printer=printer, bedcheck=bedcheck, jobs=jobs, notifier=notifier,
                       wake_runner=lambda: woken.append(True))
        client = TestClient(create_app(svc, secret_key="test-secret"))
        return Web(client, clock, holder, printer, bedcheck, jobs, notifier, woken)

    return build


@pytest.fixture
def web(make_web) -> Web:
    return make_web()
```

`tests/web/test_auth.py`:

```python
def test_pages_redirect_to_login(web):
    response = web.client.get("/files", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_htmx_requests_get_401_with_redirect_header(web):
    response = web.client.get("/partials/status", headers={"HX-Request": "true"})
    assert response.status_code == 401 and response.headers["HX-Redirect"] == "/login"


def test_wrong_then_right_password(web):
    response = web.client.post("/login", data={"password": "nope"})
    assert response.status_code == 401 and "Wrong password." in response.text
    web.login()
    assert web.client.get("/healthz").json() == {"ok": True, "printer_online": True}


def test_lockout_after_five_failures(web):
    for _ in range(5):
        web.client.post("/login", data={"password": "nope"})
    response = web.client.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert response.status_code == 429
    web.clock.advance(61)
    web.login()


def test_cross_site_post_is_refused(web):
    web.login()
    response = web.client.post("/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_health_is_public(web):
    assert web.client.get("/healthz").status_code == 200


def test_logout(web):
    web.login()
    web.client.post("/logout")
    assert web.client.get("/files", follow_redirects=False).status_code == 303
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/web -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.web'`.

- [ ] **Step 4: Implement the web foundation**

Create empty `app/web/__init__.py`.

`app/web/deps.py`:

```python
"""What the web layer needs from the rest of the app."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import Response

from app.bedcheck.service import BedCheck
from app.clock import Clock
from app.config import Settings
from app.jobs.store import JobStore
from app.notify.service import Notifier
from app.printer.models import PrinterStatus


@dataclass
class Services:
    settings: Settings
    clock: Clock
    status: Callable[[], PrinterStatus]
    printer: Any  # PrinterClient, or a test fake with the same methods
    bedcheck: BedCheck
    jobs: JobStore
    notifier: Notifier
    wake_runner: Callable[[], None]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.tz)


def services(request: Request) -> Services:
    return request.app.state.services


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200) -> Response:
    return request.app.state.templates.TemplateResponse(
        request, name, context or {}, status_code=status_code)
```

`app/web/auth.py`:

```python
"""One shared password, a signed session cookie, a login lockout and an origin check."""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app.clock import Clock

from .deps import render, services

router = APIRouter()
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")


class LoginLimiter:
    """Locks a client out for a while after too many wrong passwords."""

    def __init__(self, clock: Clock, max_failures: int = 5,
                 lockout: timedelta = timedelta(seconds=60)) -> None:
        self._clock = clock
        self._max = max_failures
        self._lockout = lockout
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, datetime] = {}

    def locked(self, client: str) -> bool:
        until = self._locked_until.get(client)
        return until is not None and self._clock.now() < until

    def failure(self, client: str) -> None:
        count = self._failures.get(client, 0) + 1
        if count >= self._max:
            self._locked_until[client] = self._clock.now() + self._lockout
            count = 0
        self._failures[client] = count

    def success(self, client: str) -> None:
        self._failures.pop(client, None)
        self._locked_until.pop(client, None)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


async def require_login(request: Request, call_next) -> Response:
    if request.method == "POST" and not _same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    if request.url.path.startswith(PUBLIC_PREFIXES) or request.session.get("auth"):
        return await call_next(request)
    if request.method == "GET" and "hx-request" not in request.headers:
        return RedirectResponse("/login", status_code=303)
    return PlainTextResponse("Login required.", status_code=401, headers={"HX-Redirect": "/login"})


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
async def login_form(request: Request) -> Response:
    return render(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, password: str = Form("")) -> Response:
    limiter: LoginLimiter = request.app.state.limiter
    client = _client(request)
    if limiter.locked(client):
        return render(request, "login.html",
                      {"error": "Too many wrong passwords. Try again in a minute."}, 429)
    expected = services(request).settings.app_password
    if hmac.compare_digest(password.encode(), expected.encode()):
        limiter.success(client)
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    limiter.failure(client)
    return render(request, "login.html", {"error": "Wrong password."}, 401)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
```

`app/web/app.py`:

```python
"""FastAPI application factory."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.printer.models import Temperature
from app.timeutil import format_local

from . import auth
from .deps import Services, services

WEB_DIR = Path(__file__).parent


def _common_context(request: Request) -> dict[str, Any]:
    logged_in = bool(request.session.get("auth"))
    return {"logged_in": logged_in,
            "unread": services(request).notifier.unread_count() if logged_in else 0}


def _temp(t: Temperature) -> str:
    if t.current is None:
        return "–"
    text = f"{t.current:.0f}°C"
    return f"{text} → {t.target:.0f}°C" if t.target else text


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def _templates(svc: Services) -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[_common_context])
    templates.env.globals["temp"] = _temp
    templates.env.filters["local"] = lambda moment: format_local(moment, svc.tz) if moment else ""
    templates.env.filters["duration"] = _duration
    return templates


def create_app(svc: Services, *, secret_key: str,
               lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.state.templates = _templates(svc)
    app.state.limiter = auth.LoginLimiter(svc.clock)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.middleware("http")(auth.require_login)
    # Added last so it wraps everything: require_login reads the session.
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="sp_session",
                       same_site="strict", https_only=False, max_age=30 * 24 * 3600)
    app.include_router(auth.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True, "printer_online": svc.status().online}

    return app
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/web -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web tests/web
git commit -m "feat: web foundation with password login, lockout and origin check"
```

---

### Task 15: Dashboard, bed panel and notifications page

**Files:**
- Create: `app/web/routes_dashboard.py`, `app/web/routes_notifications.py`
- Create: `app/web/templates/dashboard.html`, `_status.html`, `_bed.html`, `notifications.html`
- Modify: `app/web/app.py` (include the two routers)
- Test: `tests/web/test_dashboard.py`

**Interfaces:**
- Consumes: `Services`, `render`, `services` (Task 14); `BedCheck.confirm/withdraw/active/latest`, `ConfirmRefused`, `not_idle_reason` (Task 7); `Notifier.latest/mark_all_read/notify`, `TEST` (Task 11).
- Produces: routes `GET /`, `GET /partials/status` (polled every 2 s), `GET /partials/bed` (polled every 5 s), `POST /bed/confirm`, `POST /bed/withdraw`, `POST /telegram/test`, `GET /notifications`, `POST /notifications/read-all`.

- [ ] **Step 1: Write the failing tests**

`tests/web/test_dashboard.py`:

```python
from app.printer.events import PrintStarted
from app.printer.models import PrinterStatus
from tests.factories import printing_status


def test_dashboard_shows_printer_and_heads(web):
    web.login()
    page = web.client.get("/").text
    assert "Standby" in page and "Head 3: PETG" in page and "Head 4: empty" in page


def test_status_fragment_when_offline(web):
    web.holder.status = PrinterStatus.offline()
    web.login()
    assert "Printer offline" in web.client.get("/partials/status").text


def test_confirm_and_withdraw(web):
    web.watch_printer()
    web.login()
    confirmed = web.client.post("/bed/confirm").text
    assert "Confirmed empty and clean at Wed 14 Oct 2026 20:00 (UTC+02:00)" in confirmed
    withdrawn = web.client.post("/bed/withdraw").text
    assert "Last confirmation ended: withdrawn" in withdrawn


def test_confirm_refused_while_printing(web):
    web.watch_printer()
    web.holder.status = printing_status()
    web.login()
    panel = web.client.post("/bed/confirm").text
    assert "printer is printing" in panel and "disabled" in panel


def test_bed_panel_shows_why_confirmation_ended(web):
    web.watch_printer()
    web.login()
    web.client.post("/bed/confirm")
    web.event(PrintStarted(at=web.clock.now(), filename="x.gcode"))
    assert "printed since confirmation" in web.client.get("/partials/bed").text


def test_telegram_test_message_only_when_configured(make_web):
    plain = make_web()
    plain.login()
    assert "Send Telegram test message" not in plain.client.get("/").text
    assert plain.client.post("/telegram/test").status_code == 400
    telegram = make_web(telegram_bot_token="t", telegram_chat_id="1")
    telegram.login()
    assert "Send Telegram test message" in telegram.client.get("/").text
    telegram.client.post("/telegram/test")
    assert telegram.notifier.latest()[0].text == "Test message from Schedule Print"


def test_notifications_page_and_unread_badge(web):
    web.notifier.notify("job_skipped", "Skipped cube.gcode")
    web.login()
    page = web.client.get("/notifications").text
    assert "Skipped cube.gcode" in page and 'class="badge"' in page
    web.client.post("/notifications/read-all")
    assert 'class="badge"' not in web.client.get("/notifications").text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/web/test_dashboard.py -v`
Expected: FAIL. Logged-in requests to `/` and the other routes get 404, so the content assertions fail.

- [ ] **Step 3: Write the templates**

`app/web/templates/_status.html`:

```html
<section id="status" hx-get="/partials/status" hx-trigger="every 2s" hx-swap="outerHTML">
  <h2>Printer</h2>
  {% if not status.online %}
    <p class="offline">Printer offline</p>
  {% else %}
    <p><strong>{{ status.state.value|capitalize }}</strong>
      {%- if status.filename %} · {{ status.filename }}{% endif %}
      {%- if status.state.value in ("printing", "paused") %} · {{ (status.progress * 100)|round|int }}%{% endif %}
    </p>
    <p>Bed {{ temp(status.bed) }}</p>
    <ul class="heads">
      {% for head in status.heads %}
      <li><span class="swatch" style="background:#{{ head.rgb or 'cccccc' }}"></span>
        Head {{ head.head + 1 }}: {% if head.present %}{{ head.material or "unknown" }}{% else %}empty{% endif %}
        {%- if status.nozzles|length > head.head %} · {{ temp(status.nozzles[head.head]) }}{% endif %}</li>
      {% endfor %}
    </ul>
  {% endif %}
</section>
```

`app/web/templates/_bed.html`:

```html
<section id="bed" hx-get="/partials/bed" hx-trigger="every 5s" hx-swap="outerHTML">
  <h2>Bed</h2>
  {% if active %}
    <p class="ok">Confirmed empty and clean at {{ active.confirmed_at|local }}.</p>
    <form hx-post="/bed/withdraw" hx-target="#bed" hx-swap="outerHTML"><button>Withdraw</button></form>
  {% else %}
    {% if ended %}
      <p>Last confirmation ended: {% if ended.consumed_by_job_id %}used by job #{{ ended.consumed_by_job_id }}{% else %}{{ ended.end_reason }}{% endif %} ({{ ended.ended_at|local }}).</p>
    {% else %}
      <p>No confirmation yet.</p>
    {% endif %}
    <form hx-post="/bed/confirm" hx-target="#bed" hx-swap="outerHTML">
      <button {% if blocked %}disabled{% endif %}>Bed is empty and clean</button>
    </form>
    {% if blocked %}<p class="hint">Can't confirm now: {{ blocked }}.</p>{% endif %}
  {% endif %}
  {% if bed_error %}<p class="error">{{ bed_error }}</p>{% endif %}
</section>
```

`app/web/templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block content %}
{% include "_status.html" %}
{% include "_bed.html" %}
<section>
  <h2>Next job</h2>
  {% if next_job %}
    <p>{{ next_job.filename }} at {{ next_job.scheduled_at|local }} · <a href="/jobs">all jobs</a></p>
  {% else %}
    <p>Nothing scheduled. <a href="/files">Pick a file</a>.</p>
  {% endif %}
</section>
<section>
  <h2>Notifications</h2>
  {% for n in notifications %}
    <p><span class="hint">{{ n.at|local }}</span> {{ n.text }}</p>
  {% else %}
    <p class="hint">None yet.</p>
  {% endfor %}
  <p><a href="/notifications">All notifications</a></p>
  {% if telegram_enabled %}
    <form method="post" action="/telegram/test"><button>Send Telegram test message</button></form>
  {% endif %}
</section>
{% endblock %}
```

`app/web/templates/notifications.html`:

```html
{% extends "base.html" %}
{% block content %}
<section>
  <h1>Notifications</h1>
  <form method="post" action="/notifications/read-all"><button>Mark all read</button></form>
  {% for n in notifications %}
    <p>{% if not n.read %}<strong>{% endif %}<span class="hint">{{ n.at|local }}</span> {{ n.text }}{% if not n.read %}</strong>{% endif %}
      {% if n.telegram_status == "failed" %}<br><span class="error">Telegram: not delivered ({{ n.telegram_error }})</span>{% endif %}</p>
  {% else %}
    <p class="hint">None yet.</p>
  {% endfor %}
</section>
{% endblock %}
```

- [ ] **Step 4: Implement the routes**

`app/web/routes_dashboard.py`:

```python
"""Dashboard: printer status, bed confirmation, next job, latest notifications."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from app.bedcheck.service import ConfirmRefused, not_idle_reason
from app.notify.service import TEST

from .deps import Services, render, services

router = APIRouter()


def bed_context(svc: Services, error: str | None = None) -> dict:
    status = svc.status()
    active = svc.bedcheck.active()
    blocked = None if status.online and status.idle else not_idle_reason(status)
    return {"active": active, "ended": None if active else svc.bedcheck.latest(),
            "blocked": blocked, "bed_error": error}


@router.get("/")
async def dashboard(request: Request) -> Response:
    svc = services(request)
    pending = svc.jobs.pending()
    return render(request, "dashboard.html", {
        "status": svc.status(), **bed_context(svc),
        "next_job": pending[0] if pending else None,
        "notifications": svc.notifier.latest(5),
        "telegram_enabled": svc.settings.telegram_enabled,
    })


@router.get("/partials/status")
async def status_partial(request: Request) -> Response:
    return render(request, "_status.html", {"status": services(request).status()})


@router.get("/partials/bed")
async def bed_partial(request: Request) -> Response:
    return render(request, "_bed.html", bed_context(services(request)))


@router.post("/bed/confirm")
async def confirm(request: Request) -> Response:
    svc = services(request)
    try:
        svc.bedcheck.confirm()
    except ConfirmRefused as exc:
        return render(request, "_bed.html", bed_context(svc, error=f"Not confirmed: {exc}."))
    return render(request, "_bed.html", bed_context(svc))


@router.post("/bed/withdraw")
async def withdraw(request: Request) -> Response:
    svc = services(request)
    svc.bedcheck.withdraw()
    return render(request, "_bed.html", bed_context(svc))


@router.post("/telegram/test")
async def telegram_test(request: Request) -> Response:
    svc = services(request)
    if not svc.settings.telegram_enabled:
        return PlainTextResponse("Telegram is not configured.", status_code=400)
    svc.notifier.notify(TEST, "Test message from Schedule Print")
    return RedirectResponse("/", status_code=303)
```

`app/web/routes_notifications.py`:

```python
"""Full notification feed."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .deps import render, services

router = APIRouter()


@router.get("/notifications")
async def notifications_page(request: Request) -> Response:
    return render(request, "notifications.html",
                  {"notifications": services(request).notifier.latest(200)})


@router.post("/notifications/read-all")
async def read_all(request: Request) -> Response:
    services(request).notifier.mark_all_read()
    return RedirectResponse("/notifications", status_code=303)
```

In `app/web/app.py`, change the import line `from . import auth` to:

```python
from . import auth, routes_dashboard, routes_notifications
```

and after `app.include_router(auth.router)` add:

```python
    app.include_router(routes_dashboard.router)
    app.include_router(routes_notifications.router)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/web -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web tests/web/test_dashboard.py
git commit -m "feat: dashboard with live status, bed confirmation and notifications"
```

---

### Task 16: Files page, upload and thumbnails

**Files:**
- Create: `app/web/routes_files.py`, `app/web/templates/files.html`
- Modify: `app/web/app.py` (include the router)
- Test: `tests/web/test_files.py`

**Interfaces:**
- Consumes: `printer.files()`, `printer.file_info()`, `printer.upload()`, `printer.thumbnail()`, `PrinterError` (Task 4); `render`, `services` (Task 14).
- Produces: routes `GET /files`, `POST /files/upload` (multipart field `file`), `GET /files/thumbnail?path=…`. Schedule links point to `/jobs/new?file=<urlencoded name>` (Task 17).

- [ ] **Step 1: Write the failing tests**

`tests/web/test_files.py`:

```python
from app.printer.client import PrinterOffline
from app.printer.models import FileColour
from tests.factories import make_file


def test_files_page_lists_files_with_colours_and_encoded_links(web):  # Review Focus: names
    name = "plates/globe light.gcode"
    web.printer.files_by_name[name] = make_file(
        name, colours=(FileColour(0, "FF0000", "PLA"),), thumbnail="plates/.thumbs/g.png")
    web.login()
    page = web.client.get("/files").text
    assert name in page
    assert 'href="/jobs/new?file=plates/globe%20light.gcode"' in page
    assert "/files/thumbnail?path=plates/.thumbs/g.png" in page
    assert "background:#FF0000" in page


def test_printer_offline_shows_message(web):  # Review Focus: printer errors
    web.printer.fail_on["files"] = PrinterOffline("printer unreachable: ConnectError")
    web.login()
    response = web.client.get("/files")
    assert response.status_code == 200
    assert "Could not read files from the printer: printer unreachable" in response.text


def test_upload_strips_folders_and_redirects(web):
    web.login()
    response = web.client.post("/files/upload", files={"file": ("../../evil/part.gcode", b"G28\n")},
                               follow_redirects=False)
    assert response.status_code == 303
    assert web.printer.uploaded == {"part.gcode": b"G28\n"}


def test_upload_rejects_other_files(web):
    web.login()
    response = web.client.post("/files/upload", files={"file": ("photo.jpg", b"x")})
    assert response.status_code == 400 and "Choose a .gcode file." in response.text
    assert web.printer.uploaded == {}


def test_thumbnail_proxy_and_traversal(web):
    web.login()
    assert web.client.get("/files/thumbnail", params={"path": "a/.thumbs/x.png"}).content == b"PNG"
    assert web.client.get("/files/thumbnail", params={"path": "../secret"}).status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/web/test_files.py -v`
Expected: FAIL. `/files` returns 404 after login.

- [ ] **Step 3: Implement**

`app/web/templates/files.html`:

```html
{% extends "base.html" %}
{% block content %}
<section>
  <h1>Files on the printer</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <table>
    <tr><th></th><th>File</th><th>Print time</th><th>Colours</th><th></th></tr>
    {% for f in files %}
    <tr>
      <td>{% if f.thumbnail %}<img class="thumb" src="/files/thumbnail?path={{ f.thumbnail|urlencode }}" alt="">{% endif %}</td>
      <td>{{ f.filename }}</td>
      <td>{{ f.estimated_seconds|duration }}</td>
      <td>{% for c in f.colours %}<span class="swatch" style="background:#{{ c.rgb or 'cccccc' }}" title="{{ c.material or 'unknown material' }}"></span> {% endfor %}</td>
      <td><a href="/jobs/new?file={{ f.filename|urlencode }}">Schedule</a></td>
    </tr>
    {% else %}
    <tr><td colspan="5" class="hint">No G-code files on the printer.</td></tr>
    {% endfor %}
  </table>
</section>
<section>
  <h2>Upload</h2>
  <form method="post" action="/files/upload" enctype="multipart/form-data">
    <input type="file" name="file" accept=".gcode" required>
    <button>Upload without printing</button>
  </form>
  {% if upload_error %}<p class="error">{{ upload_error }}</p>{% endif %}
</section>
{% endblock %}
```

`app/web/routes_files.py`:

```python
"""G-code files on the printer: list, upload, thumbnails."""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app.printer.client import PrinterError

from .deps import render, services

router = APIRouter()


async def _files_page(request: Request, upload_error: str | None = None,
                      status_code: int = 200) -> Response:
    svc = services(request)
    files, error = [], None
    try:
        for entry in await svc.printer.files():
            info = await svc.printer.file_info(entry.filename)
            if info is not None:
                files.append(info)
    except PrinterError as exc:
        error = f"Could not read files from the printer: {exc}"
    return render(request, "files.html",
                  {"files": files, "error": error, "upload_error": upload_error}, status_code)


@router.get("/files")
async def files_page(request: Request) -> Response:
    return await _files_page(request)


@router.post("/files/upload")
async def upload(request: Request, file: UploadFile) -> Response:
    name = PurePosixPath((file.filename or "").replace("\\", "/")).name
    if not name.lower().endswith(".gcode"):
        return await _files_page(request, "Choose a .gcode file.", 400)
    try:
        await services(request).printer.upload(name, await file.read())
    except PrinterError as exc:
        return await _files_page(request, f"Upload failed: {exc}", 502)
    return RedirectResponse("/files", status_code=303)


@router.get("/files/thumbnail")
async def thumbnail(request: Request, path: str) -> Response:
    if path.startswith("/") or ".." in path.split("/"):
        return Response(status_code=400)
    try:
        data = await services(request).printer.thumbnail(path)
    except PrinterError:
        return Response(status_code=502)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
```

In `app/web/app.py`, add `routes_files` to the router import and add `app.include_router(routes_files.router)` after the other routers.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/web -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web tests/web/test_files.py
git commit -m "feat: files page with upload and thumbnail proxy"
```

---

### Task 17: Schedule form and jobs page

**Files:**
- Create: `app/web/routes_jobs.py`, `app/web/templates/job_form.html`, `app/web/templates/jobs.html`
- Modify: `app/web/app.py` (include the router)
- Test: `tests/web/test_jobs.py`

**Interfaces:**
- Consumes: `JobStore` and `JobNotEditable` (Task 8); `suggest_heads`, `build_assignments`, `HeadChoiceError` (Task 9); `parse_local_input`, `local_to_utc`, `format_local`, `format_local_input`, `LocalTimeError` (Task 8); `PrinterError` (Task 4); `render`, `services` (Task 14).
- Produces: routes `GET /jobs`, `GET /jobs/new?file=…`, `POST /jobs`, `GET /jobs/{id}/edit`, `POST /jobs/{id}`, `POST /jobs/{id}/cancel`. Form fields: `filename`, `scheduled_local` (datetime-local), `head_<logical>` (0–3), optional `confirm_overlap=1`.

- [ ] **Step 1: Write the failing tests**

`tests/web/test_jobs.py`:

```python
from datetime import datetime, timezone

import pytest

from app.jobs.models import HeadAssignment, JobStatus
from app.printer.models import PrinterStatus
from tests.factories import make_file


def form(**overrides) -> dict:
    values = {"filename": "cube.gcode", "scheduled_local": "2026-10-15T02:00", "head_0": "0"}
    values.update(overrides)
    return values


def test_new_job_form_preselects_suggested_head(web):
    web.login()
    page = web.client.get("/jobs/new", params={"file": "cube.gcode"})
    assert page.status_code == 200
    assert '<option value="0" selected' in page.text  # white PLA file → white PLA head
    assert "Times are in Europe/Berlin" in page.text


def test_create_job_stores_utc_and_wakes_runner(web):
    web.login()
    response = web.client.post("/jobs", data=form(), follow_redirects=False)
    assert response.status_code == 303
    [job] = web.jobs.pending()
    assert job.scheduled_at == datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    assert job.head_map == (HeadAssignment(0, 0, "PLA", "FFFFFF"),)
    assert web.woken


@pytest.mark.parametrize("value, message", [
    ("2026-10-14T19:00", "That time is in the past."),
    ("", "Pick a date and time."),
    ("2027-03-28T02:30", "does not exist"),
])
def test_bad_times_are_form_errors(web, value, message):  # Review Focus: bad schedule input
    web.login()
    response = web.client.post("/jobs", data=form(scheduled_local=value))
    assert response.status_code == 400 and message in response.text
    assert web.jobs.pending() == []


def test_empty_head_is_rejected(web):
    web.login()
    response = web.client.post("/jobs", data=form(head_0="3"))
    assert response.status_code == 400 and "Head 4 is empty." in response.text


def test_offline_printer_blocks_scheduling(web):  # Review Focus: printer errors
    web.holder.status = PrinterStatus.offline()
    web.login()
    response = web.client.post("/jobs", data=form())
    assert response.status_code == 400 and "printer is offline" in response.text


def test_overlap_warning_then_schedule_anyway(web):
    web.login()
    web.client.post("/jobs", data=form(scheduled_local="2026-10-15T01:30"))
    warned = web.client.post("/jobs", data=form())  # 30 min later; the file takes an hour
    assert "may still be printing then" in warned.text and len(web.jobs.pending()) == 1
    web.client.post("/jobs", data=form(confirm_overlap="1"))
    assert len(web.jobs.pending()) == 2


def test_edit_and_cancel(web):
    web.login()
    web.client.post("/jobs", data=form())
    [job] = web.jobs.pending()
    assert 'value="2026-10-15T02:00"' in web.client.get(f"/jobs/{job.id}/edit").text
    web.client.post(f"/jobs/{job.id}", data=form(scheduled_local="2026-10-15T03:00"))
    assert web.jobs.get(job.id).scheduled_at == datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)
    web.client.post(f"/jobs/{job.id}/cancel")
    assert web.jobs.get(job.id).status is JobStatus.CANCELLED


def test_acting_on_finished_job_is_refused(web):  # Review Focus: finished job
    web.login()
    web.client.post("/jobs", data=form())
    [job] = web.jobs.pending()
    web.jobs.set_status(job.id, JobStatus.STARTED)
    assert web.client.get(f"/jobs/{job.id}/edit").status_code == 409
    assert web.client.post(f"/jobs/{job.id}", data=form()).status_code == 409
    assert web.client.post(f"/jobs/{job.id}/cancel").status_code == 409
    assert web.jobs.get(job.id).status is JobStatus.STARTED


def test_jobs_page_escapes_file_names(web):  # Review Focus: names
    name = "<b>bold</b>.gcode"
    web.printer.files_by_name[name] = make_file(name)
    web.login()
    web.client.post("/jobs", data=form(filename=name))
    page = web.client.get("/jobs").text
    assert "&lt;b&gt;bold&lt;/b&gt;.gcode" in page and "<b>bold</b>" not in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/web/test_jobs.py -v`
Expected: FAIL. `/jobs` routes return 404.

- [ ] **Step 3: Write the templates**

`app/web/templates/job_form.html`:

```html
{% extends "base.html" %}
{% block content %}
<section>
  <h1>{{ title }}</h1>
  <p><strong>{{ file.filename }}</strong> · {{ file.estimated_seconds|duration }}</p>
  <form method="post" action="{{ action }}">
    <input type="hidden" name="filename" value="{{ file.filename }}">
    <label>Start at <input type="datetime-local" name="scheduled_local" value="{{ scheduled_local }}" required></label>
    <p class="hint">Times are in {{ tz_name }}.</p>
    {% for colour in file.colours %}
    <fieldset>
      <legend><span class="swatch" style="background:#{{ colour.rgb or 'cccccc' }}"></span>
        Colour {{ colour.logical + 1 }} ({{ colour.material or "unknown material" }})</legend>
      <select name="head_{{ colour.logical }}">
        {% for head in heads %}
        <option value="{{ head.head }}" {% if choice.get(colour.logical) == head.head %}selected{% endif %} {% if not head.present %}disabled{% endif %}>Head {{ head.head + 1 }}: {% if head.present %}{{ head.material or "unknown" }}{% if head.rgb %} #{{ head.rgb }}{% endif %}{% else %}empty{% endif %}</option>
        {% endfor %}
      </select>
    </fieldset>
    {% endfor %}
    {% for e in errors %}<p class="error">{{ e }}</p>{% endfor %}
    {% for w in warnings %}<p class="warning">{{ w }}</p>{% endfor %}
    {% if warnings %}
      <input type="hidden" name="confirm_overlap" value="1">
      <button>Schedule anyway</button>
    {% else %}
      <button>Save</button>
    {% endif %}
  </form>
</section>
{% endblock %}
```

`app/web/templates/jobs.html`:

```html
{% extends "base.html" %}
{% block content %}
<section>
  <h1>Upcoming</h1>
  {% if upcoming %}
  <table>
    <tr><th>When</th><th>File</th><th>Heads</th><th></th></tr>
    {% for job in upcoming %}
    <tr>
      <td>{{ job.scheduled_at|local }}</td>
      <td>{{ job.filename }}</td>
      <td>{% for a in job.head_map %}<span class="swatch" style="background:#{{ a.expected_rgb or 'cccccc' }}"></span> Head {{ a.head + 1 }} {% endfor %}</td>
      <td><a href="/jobs/{{ job.id }}/edit">Change</a>
        <form method="post" action="/jobs/{{ job.id }}/cancel" class="inline"><button class="link">Cancel</button></form></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>Nothing scheduled. <a href="/files">Pick a file</a>.</p>
  {% endif %}
</section>
<section>
  <h2>History</h2>
  {% if finished %}
  <table>
    <tr><th>When</th><th>File</th><th>Result</th></tr>
    {% for job in finished %}
    <tr><td>{{ job.scheduled_at|local }}</td><td>{{ job.filename }}</td>
      <td>{{ job.status.value }}{% if job.outcome_reason %}: {{ job.outcome_reason }}{% endif %}</td></tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="hint">No finished jobs yet.</p>
  {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 4: Implement the routes**

`app/web/routes_jobs.py`:

```python
"""Scheduling, changing and cancelling jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from app.jobs.heads import HeadChoiceError, build_assignments, suggest_heads
from app.jobs.models import HeadAssignment, JobStatus
from app.jobs.store import JobNotEditable
from app.printer.client import PrinterError
from app.printer.models import FileInfo
from app.timeutil import (
    LocalTimeError, format_local, format_local_input, local_to_utc, parse_local_input,
)

from .deps import Services, render, services

router = APIRouter()
NOT_EDITABLE = "This job already ran or was cancelled, so it can't be changed."
OFFLINE = ("The printer is offline, so the loaded filament can't be checked. "
           "Try again when it is online.")


@dataclass
class Checked:
    info: FileInfo
    when: datetime | None = None
    head_map: tuple[HeadAssignment, ...] = ()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _message(request: Request, text: str, status_code: int) -> Response:
    return render(request, "message.html", {"message": text}, status_code)


def _choice(form: FormData, info: FileInfo) -> dict[int, int]:
    choice = {}
    for colour in info.colours:
        raw = str(form.get(f"head_{colour.logical}", ""))
        if raw.isdigit():
            choice[colour.logical] = int(raw)
    return choice


async def _load_file(svc: Services, filename: str) -> FileInfo | str:
    """The file's info, or an error message for the user."""
    try:
        info = await svc.printer.file_info(filename)
    except PrinterError as exc:
        return f"Could not read the file from the printer: {exc}"
    return info if info is not None else f"{filename} is not on the printer."


async def _overlap_warnings(svc: Services, when: datetime, job_id: int | None) -> list[str]:
    previous = svc.jobs.previous_pending(when, exclude_id=job_id)
    if previous is None:
        return []
    try:
        info = await svc.printer.file_info(previous.filename)
    except PrinterError:
        return []
    if info is None or not info.estimated_seconds:
        return []
    if previous.scheduled_at + timedelta(seconds=info.estimated_seconds) <= when:
        return []
    return [f"{previous.filename} starts at {format_local(previous.scheduled_at, svc.tz)} and may "
            "still be printing then. This job will be skipped unless you clear the bed and "
            "confirm it again in between."]


async def _check(svc: Services, form: FormData, info: FileInfo, job_id: int | None = None) -> Checked:
    checked = Checked(info)
    try:
        checked.when = local_to_utc(parse_local_input(str(form.get("scheduled_local", ""))), svc.tz)
        if checked.when <= svc.clock.now():
            checked.errors.append("That time is in the past.")
    except LocalTimeError as exc:
        checked.errors.append(str(exc))
    status = svc.status()
    if not status.online:
        checked.errors.append(OFFLINE)
    else:
        try:
            checked.head_map = build_assignments(_choice(form, info), info.colours, status.heads)
        except HeadChoiceError as exc:
            checked.errors.append(f"{str(exc).capitalize()}.")
    if not checked.errors and checked.when is not None:
        checked.warnings = await _overlap_warnings(svc, checked.when, job_id)
    return checked


def _form(request: Request, *, info: FileInfo, action: str, title: str, scheduled_local: str,
          choice: dict[int, int], errors=(), warnings=(), status_code: int = 200) -> Response:
    svc = services(request)
    return render(request, "job_form.html", {
        "file": info, "action": action, "title": title, "scheduled_local": scheduled_local,
        "choice": choice, "heads": svc.status().heads, "errors": list(errors),
        "warnings": list(warnings), "tz_name": svc.settings.tz,
    }, status_code)


def _needs_another_look(form: FormData, checked: Checked) -> bool:
    return bool(checked.errors) or (bool(checked.warnings) and form.get("confirm_overlap") != "1")


@router.get("/jobs")
async def jobs_page(request: Request) -> Response:
    svc = services(request)
    return render(request, "jobs.html", {"upcoming": svc.jobs.pending(), "finished": svc.jobs.finished(50)})


@router.get("/jobs/new")
async def new_job(request: Request, file: str = "") -> Response:
    svc = services(request)
    info = await _load_file(svc, file)
    if isinstance(info, str):
        return _message(request, info, 404)
    status = svc.status()
    return _form(request, info=info, action="/jobs", title="Schedule a print", scheduled_local="",
                 choice=suggest_heads(info.colours, status.heads),
                 errors=[] if status.online else [OFFLINE])


@router.post("/jobs")
async def create_job(request: Request) -> Response:
    svc = services(request)
    form = await request.form()
    info = await _load_file(svc, str(form.get("filename", "")))
    if isinstance(info, str):
        return _message(request, info, 400)
    checked = await _check(svc, form, info)
    if _needs_another_look(form, checked):
        return _form(request, info=info, action="/jobs", title="Schedule a print",
                     scheduled_local=str(form.get("scheduled_local", "")), choice=_choice(form, info),
                     errors=checked.errors, warnings=checked.warnings,
                     status_code=400 if checked.errors else 200)
    svc.jobs.create(filename=info.filename, file_size=info.size, file_modified=info.modified,
                    scheduled_at=checked.when, head_map=checked.head_map)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs/{job_id}/edit")
async def edit_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    job = svc.jobs.get(job_id)
    if job is None or job.status is not JobStatus.PENDING:
        return _message(request, NOT_EDITABLE, 409)
    info = await _load_file(svc, job.filename)
    if isinstance(info, str):
        return _message(request, info, 404)
    return _form(request, info=info, action=f"/jobs/{job_id}", title="Change a scheduled print",
                 scheduled_local=format_local_input(job.scheduled_at, svc.tz),
                 choice={a.logical: a.head for a in job.head_map})


@router.post("/jobs/{job_id}")
async def update_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    job = svc.jobs.get(job_id)
    if job is None or job.status is not JobStatus.PENDING:
        return _message(request, NOT_EDITABLE, 409)
    form = await request.form()
    info = await _load_file(svc, job.filename)  # the job's own file, whatever the form says
    if isinstance(info, str):
        return _message(request, info, 400)
    checked = await _check(svc, form, info, job_id=job_id)
    if _needs_another_look(form, checked):
        return _form(request, info=info, action=f"/jobs/{job_id}", title="Change a scheduled print",
                     scheduled_local=str(form.get("scheduled_local", "")), choice=_choice(form, info),
                     errors=checked.errors, warnings=checked.warnings,
                     status_code=400 if checked.errors else 200)
    try:
        svc.jobs.update(job_id, scheduled_at=checked.when, head_map=checked.head_map,
                        file_size=info.size, file_modified=info.modified)
    except JobNotEditable:
        return _message(request, NOT_EDITABLE, 409)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int) -> Response:
    svc = services(request)
    try:
        svc.jobs.cancel(job_id)
    except JobNotEditable:
        return _message(request, NOT_EDITABLE, 409)
    svc.wake_runner()
    return RedirectResponse("/jobs", status_code=303)
```

In `app/web/app.py`, add `routes_jobs` to the router import and add `app.include_router(routes_jobs.router)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/web -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web tests/web/test_jobs.py
git commit -m "feat: schedule form with head choice, DST checks, overlap warning and jobs page"
```

---

### Task 18: Wiring, entry point and Docker

**Files:**
- Create: `app/main.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`
- Test: `tests/unit/test_main.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Runtime` dataclass (`settings, clock, conn, printer, bus, watcher, bedcheck, jobs, notifier, runner, sender, delivery`); `configure_logging()`; `build_runtime(settings, *, clock=None, printer_transport=None, ws_connect=None, telegram_transport=None) -> Runtime`; `create_app_from_runtime(rt) -> FastAPI`; `create_app_from_env() -> FastAPI` (uvicorn factory). Wires `print_finished` and `confirmation_voided` notifications (spec §9).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_main.py`:

```python
import asyncio
import logging
import time
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, configure_logging, create_app_from_runtime
from app.notify.service import CONFIRMATION_VOIDED, PRINT_FINISHED
from app.printer.events import Connected, PrintFinished, PrintStarted
from tests.factories import IDLE_OBJECTS, make_status
from tests.fakes import FakeWS, Router, connector, ok


def settings_for(tmp_path) -> Settings:
    return Settings(printer_host="printer", app_password="secret", tz="UTC", data_dir=tmp_path)


def test_logging_hides_request_urls():
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_printer_events_become_notifications(tmp_path, clock):
    rt = build_runtime(settings_for(tmp_path), clock=clock)
    rt.watcher.status = make_status()

    async def scenario():
        await rt.bus.publish(Connected(at=clock.now(), status=rt.watcher.status))
        rt.bedcheck.confirm()
        rt.jobs.create(filename="cube.gcode", file_size=1, file_modified=1.0,
                       scheduled_at=clock.now() + timedelta(hours=2), head_map=())
        await rt.bus.publish(PrintStarted(at=clock.now(), filename="other.gcode"))
        await rt.bus.publish(PrintFinished(at=clock.now(), filename="other.gcode", result="complete"))

    asyncio.run(scenario())
    kinds = [n.kind for n in reversed(rt.notifier.latest(10))]
    assert kinds == [CONFIRMATION_VOIDED, PRINT_FINISHED]
    rt.conn.close()


def test_app_starts_watches_and_stops(tmp_path):
    router = Router({("GET", "/server/files/list"): ok([])})
    rt = build_runtime(settings_for(tmp_path), printer_transport=httpx.MockTransport(router),
                       ws_connect=connector(FakeWS(IDLE_OBJECTS)))
    with TestClient(create_app_from_runtime(rt)) as client:
        deadline = time.monotonic() + 5
        while not client.get("/healthz").json()["printer_online"]:
            assert time.monotonic() < deadline, "watcher never connected"
            time.sleep(0.05)
        client.post("/login", data={"password": "secret"})
        assert "Head 1: PLA" in client.get("/").text
        assert client.get("/files").status_code == 200
    assert (tmp_path / "app.db").exists() and (tmp_path / "secret_key").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Implement the wiring**

`app/main.py`:

```python
"""Wiring: build every part, run the background tasks, serve the app."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from app.bedcheck.service import BedCheck
from app.clock import Clock, SystemClock
from app.config import Settings, load_or_create_secret
from app.db import connect, migrate
from app.jobs.runner import Runner, RunnerSettings
from app.jobs.store import JobStore
from app.notify import messages
from app.notify.service import CONFIRMATION_VOIDED, PRINT_FINISHED, Notifier
from app.notify.telegram import DeliveryWorker, TelegramSender
from app.printer.client import PrinterClient
from app.printer.events import Event, EventBus, PrintFinished
from app.printer.watcher import PrinterWatcher
from app.web.app import create_app
from app.web.deps import Services


@dataclass
class Runtime:
    settings: Settings
    clock: Clock
    conn: sqlite3.Connection
    printer: PrinterClient
    bus: EventBus
    watcher: PrinterWatcher
    bedcheck: BedCheck
    jobs: JobStore
    notifier: Notifier
    runner: Runner
    sender: TelegramSender | None
    delivery: DeliveryWorker | None


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_runtime(settings: Settings, *, clock: Clock | None = None,
                  printer_transport: httpx.AsyncBaseTransport | None = None, ws_connect=None,
                  telegram_transport: httpx.AsyncBaseTransport | None = None) -> Runtime:
    clock = clock or SystemClock()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.data_dir / "app.db")
    migrate(conn)
    printer = PrinterClient(settings.printer_base_url, settings.printer_api_key,
                            transport=printer_transport)
    bus = EventBus()
    watcher = PrinterWatcher(settings.printer_ws_url, bus, clock,
                             api_key=settings.printer_api_key, connect=ws_connect)
    jobs = JobStore(conn, clock)
    notifier = Notifier(conn, clock, telegram_enabled=settings.telegram_enabled)
    bedcheck = BedCheck(conn, clock, printer, lambda: watcher.status)
    runner = Runner(
        jobs=jobs, bedcheck=bedcheck, printer=printer, status=lambda: watcher.status,
        notifier=notifier, clock=clock,
        settings=RunnerSettings(
            tz=ZoneInfo(settings.tz), dry_run=settings.dry_run,
            offline_grace=timedelta(minutes=settings.offline_grace_minutes),
            start_verify_seconds=settings.start_verify_seconds,
            reminder=timedelta(minutes=settings.reminder_minutes),
        ),
    )

    async def on_voided(reason: str) -> None:
        pending = jobs.count_pending()
        if pending:
            notifier.notify(CONFIRMATION_VOIDED, messages.confirmation_voided(reason, pending))

    async def on_event(event: Event) -> None:
        if isinstance(event, PrintFinished):
            notifier.notify(PRINT_FINISHED, messages.print_finished(event.filename, event.result))

    bedcheck.set_void_listener(on_voided)
    bus.subscribe(bedcheck.handle_event)
    bus.subscribe(on_event)

    sender = delivery = None
    if settings.telegram_enabled:
        sender = TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id,
                                transport=telegram_transport)
        delivery = DeliveryWorker(notifier, sender, clock, app_base_url=settings.app_base_url)
        notifier.add_listener(delivery.wake)
    return Runtime(settings, clock, conn, printer, bus, watcher, bedcheck, jobs, notifier, runner,
                   sender, delivery)


def create_app_from_runtime(rt: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt.bedcheck.startup()
        tasks = [asyncio.create_task(rt.watcher.run(), name="printer-watcher"),
                 asyncio.create_task(rt.runner.run(), name="job-runner")]
        if rt.delivery is not None:
            tasks.append(asyncio.create_task(rt.delivery.run(), name="telegram-delivery"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            rt.bedcheck.shutdown()
            await rt.printer.close()
            if rt.sender is not None:
                await rt.sender.close()
            rt.conn.close()

    svc = Services(settings=rt.settings, clock=rt.clock, status=lambda: rt.watcher.status,
                   printer=rt.printer, bedcheck=rt.bedcheck, jobs=rt.jobs, notifier=rt.notifier,
                   wake_runner=rt.runner.wake)
    return create_app(svc, secret_key=load_or_create_secret(rt.settings), lifespan=lifespan)


def create_app_from_env() -> FastAPI:
    """uvicorn entry point: `uvicorn --factory app.main:create_app_from_env`."""
    configure_logging()
    return create_app_from_runtime(build_runtime(Settings.from_env()))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest -v`
Expected: all unit and web tests PASS (integration tests deselected).

- [ ] **Step 5: Add the Docker and documentation files**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "--factory", "app.main:create_app_from_env", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yml`:

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - ./data:/data
```

`.env.example`:

```
# Required
PRINTER_HOST=192.168.1.50
APP_PASSWORD=change-me
TZ=Europe/Berlin
# Keep true until a dry run has passed on your printer
DRY_RUN=true

# Optional
# PRINTER_API_KEY=
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
# APP_BASE_URL=http://raspberrypi.local:8000
# OFFLINE_GRACE_MINUTES=10
# START_VERIFY_SECONDS=90
# REMINDER_MINUTES=60
```

`README.md`:

````markdown
# Schedule Print

Starts prints on a Snapmaker U1 at a time you choose, but only if someone has
confirmed the bed is empty and clean and the printer hasn't printed since.
It runs on an always-on box on your home network.

## Set up

1. Give the printer a DHCP reservation in your router so its address never changes.
2. On the home box (with Docker): clone this repository, `cp .env.example .env`, edit `.env`.
3. `docker compose up -d --build`, then open `http://<home-box>:8000`.
4. Keep `DRY_RUN=true` for your first scheduled job. It runs every check and logs what it
   would send, without starting anything. Then set `DRY_RUN=false` and run
   `docker compose up -d` again.

## Telegram (optional, send-only)

1. In Telegram, message @BotFather, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<token>/getUpdates` and copy `message.chat.id`.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, restart, and press
   "Send Telegram test message" on the dashboard.

The app only sends messages. Nothing written to the bot can affect the printer.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER_HOST` | required | Printer IP or name |
| `APP_PASSWORD` | required | Password for the web page |
| `TZ` | `UTC` | Time zone for entering and showing times |
| `DRY_RUN` | `false` | Run all checks but never start a print |
| `PRINTER_PORT` | `7125` | Moonraker port |
| `PRINTER_API_KEY` | empty | Only if your printer requires one |
| `OFFLINE_GRACE_MINUTES` | `10` | How late a job may still start if the printer was briefly offline |
| `START_VERIFY_SECONDS` | `90` | How long to wait for the printer to report printing |
| `REMINDER_MINUTES` | `60` | Remind this long before a job whose bed isn't confirmed |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Both needed to enable Telegram |
| `APP_BASE_URL` | empty | Link added to Telegram messages |
| `SECRET_KEY` | generated | Session signing key (stored in `/data` if unset) |

## How the safety rule works

- "Bed is empty and clean" can only be confirmed while the printer is idle.
- Any print that starts afterwards voids the confirmation, whether it was started from the
  touchscreen, the Snapmaker app or a scheduled job. Every start attempt by a scheduled
  job uses it up, even if the start fails.
- If the app stopped watching the printer for more than 2 minutes, it checks the printer's
  job history for that time. If it can't prove the printer stayed idle, it voids the
  confirmation.
- A job never starts more than `OFFLINE_GRACE_MINUTES` late.

## Development

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -e '.[dev]'
pytest
```

Integration tests against a simulated U1: see `tests/integration/README.md`.
````

- [ ] **Step 6: Smoke-test the container**

Run:

```bash
docker build -t schedule-print . && \
docker run --rm -d --name sp-smoke -e PRINTER_HOST=192.0.2.1 -e APP_PASSWORD=x -e DATA_DIR=/tmp/data -p 8000:8000 schedule-print && \
curl -s --retry 15 --retry-connrefused --retry-delay 1 http://localhost:8000/healthz; \
docker stop sp-smoke
```

Expected: `{"ok":true,"printer_online":false}` (the address has no printer), then the container stops.

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/unit/test_main.py Dockerfile docker-compose.yml .env.example README.md
git commit -m "feat: wire services, background tasks, Docker image and README"
```

---

### Task 19: Integration tests against the U1 simulator

**Files:**
- Create: `docker-compose.test.yml`, `scripts/sim-up.sh`, `tests/fixtures/two_colour.gcode`
- Create: `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/README.md`
- Test: `tests/integration/test_u1sim.py`

**Interfaces:**
- Consumes: `PrinterClient` (Task 4), `PrinterWatcher`, `EventBus` (Task 5), `eventually`, `stop` (Task 5 fakes).
- Produces: a `sim_url` fixture that only allows `localhost`/`127.0.0.1` (the spec §12 safety fence), and `scripts/sim-up.sh`, which starts u1sim with Snapmaker's real Moonraker on `localhost:7125`.

- [ ] **Step 1: Add the simulator setup**

`scripts/sim-up.sh` (then `chmod +x scripts/sim-up.sh`):

```bash
#!/usr/bin/env bash
# Start u1sim + Snapmaker's real Moonraker fork on localhost:7125 for integration tests.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor
if [ ! -d vendor/u1-companion ]; then
  git clone https://github.com/zkasuran/u1-companion vendor/u1-companion
  git -C vendor/u1-companion checkout 69de50e
fi
if [ ! -d vendor/u1-companion/vendor/u1-moonraker ]; then
  git clone https://github.com/Snapmaker/U1-Moonraker vendor/u1-companion/vendor/u1-moonraker
  git -C vendor/u1-companion/vendor/u1-moonraker checkout 3b8357c
fi
docker compose -f docker-compose.test.yml up -d --build
curl -s --retry 30 --retry-connrefused --retry-delay 1 http://localhost:7125/server/info > /dev/null
echo "u1sim is up on http://localhost:7125"
```

If either pinned commit no longer exists, check out the repository's latest commit and write the SHA you used into `tests/integration/README.md`.

`docker-compose.test.yml`:

```yaml
# Fake U1 for integration tests: u1sim + Snapmaker's real Moonraker fork.
# Start with scripts/sim-up.sh; Moonraker then answers on localhost:7125.
include:
  - path: vendor/u1-companion/docker-compose.yml
```

`tests/fixtures/two_colour.gcode`:

```
; schedule-print integration test file
; filament used [mm] = 120.5, 30.2
G28
T0
G1 X10 Y10 F3000
T1
G1 X20 Y20 F3000
; CONFIG_BLOCK_START
; filament_colour = #FF0000;#0000FF
; filament_type = PLA;PETG
; CONFIG_BLOCK_END
```

`tests/integration/README.md`:

```markdown
# Integration tests (u1sim)

These run the printer client and watcher against u1sim, a fake Klippy running
Snapmaker's real Moonraker fork (github.com/zkasuran/u1-companion). They never
touch a real printer: the `sim_url` fixture refuses any host except localhost.

    scripts/sim-up.sh
    pytest -m integration -v
    docker compose -f docker-compose.test.yml down

Pinned versions: u1-companion 69de50e, U1-Moonraker 3b8357c.

If a test fails because of how the simulator behaves rather than our code,
mark it `@pytest.mark.xfail(reason="u1sim: <what it does>")` and note it here.
```

- [ ] **Step 2: Write the integration tests**

Create empty `tests/integration/__init__.py`.

`tests/integration/conftest.py`:

```python
import os
from urllib.parse import urlsplit

import httpx
import pytest

SIM_URL = os.environ.get("U1SIM_URL", "http://localhost:7125")
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}


@pytest.fixture(scope="session")
def sim_url() -> str:
    host = urlsplit(SIM_URL).hostname
    if host not in ALLOWED_HOSTS:
        pytest.exit(f"refusing to run integration tests against {host}: "
                    "only the local simulator is allowed", returncode=2)
    try:
        httpx.get(f"{SIM_URL}/server/info", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("u1sim is not running; start it with scripts/sim-up.sh")
    return SIM_URL
```

`tests/integration/test_u1sim.py`:

```python
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.clock import SystemClock
from app.printer.client import PrinterClient
from app.printer.events import Connected, EventBus
from app.printer.watcher import PrinterWatcher
from tests.fakes import eventually, stop

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).parents[1] / "fixtures" / "two_colour.gcode"


@pytest.fixture
async def printer(sim_url):
    client = PrinterClient(sim_url)
    yield client
    await client.close()


async def upload_fixture(printer: PrinterClient) -> str:
    name = f"schedule-print-test-{uuid.uuid4().hex[:8]}.gcode"
    await printer.upload(name, FIXTURE.read_bytes())
    return name


async def require_idle(printer: PrinterClient) -> None:
    status = await printer.status()
    if not status.idle:
        pytest.skip(f"simulator is busy ({status.state.value}); its job timeline loops")


async def test_status_has_four_heads(printer):
    status = await printer.status()
    assert status.online and status.klippy_ready and len(status.heads) == 4


async def test_upload_lists_file_and_reads_colours(printer):
    name = await upload_fixture(printer)
    assert name in [f.filename for f in await printer.files()]
    info = await printer.file_info(name)
    assert [(c.logical, c.rgb, c.material) for c in info.colours] == [
        (0, "FF0000", "PLA"), (1, "0000FF", "PETG")]


async def test_head_map_round_trip(printer):
    await require_idle(printer)
    await printer.apply_head_map([(0, 1), (1, 3)])
    await printer.apply_head_map([(0, 0), (1, 1)])


async def test_history_answers_without_raising(printer):
    result = await printer.history(datetime.now(timezone.utc) - timedelta(days=1))
    assert result is None or isinstance(result, list)


async def test_watcher_connects(sim_url):
    bus, seen = EventBus(), []

    async def collect(event):
        seen.append(event)

    bus.subscribe(collect)
    watcher = PrinterWatcher(sim_url.replace("http://", "ws://") + "/websocket", bus, SystemClock())
    task = asyncio.create_task(watcher.run())
    try:
        await eventually(lambda: any(isinstance(e, Connected) for e in seen), timeout=10)
        assert watcher.status.online
    finally:
        await stop(task)


async def test_start_print_is_accepted(printer):
    name = await upload_fixture(printer)
    await require_idle(printer)
    await printer.start_print(name)
```

- [ ] **Step 3: Check that the tests are skipped without the simulator**

Run: `pytest -m integration -v`
Expected: every test SKIPPED with "u1sim is not running".

- [ ] **Step 4: Run against the simulator**

Run: `scripts/sim-up.sh && pytest -m integration -v`
Expected: PASS, or SKIPPED with "simulator is busy" where the simulator's looping job is running. For any failure, decide whether our code or the simulator is wrong. Fix our code, or mark the test `xfail` with the observed simulator behaviour as described in `tests/integration/README.md`.

- [ ] **Step 5: Stop the simulator and commit**

```bash
docker compose -f docker-compose.test.yml down
git add docker-compose.test.yml scripts/sim-up.sh tests/fixtures/two_colour.gcode tests/integration
git commit -m "test: integration tests against u1sim with a localhost-only fence"
```

---

### Task 20: Contract tests against the real printer's recordings

**Files:**
- Create (by running): `tests/fixtures/u1sim/*`
- Create: `tests/contract/__init__.py`
- Test: `tests/contract/test_real_u1.py`
- Modify (only if a test fails): `app/printer/u1.py`

**Interfaces:**
- Consumes: `tests/fixtures/real_u1/{idle,printing,paused,complete,start_test}` (probe plan), `tools/probe_u1.py` (probe plan), `u1` parsers (Task 3).
- Produces: tests that fail if the parsers or u1sim drift from what the real U1 sends.

- [ ] **Step 1: Record the simulator's responses with the probe tool**

Run: `scripts/sim-up.sh && uv run tools/probe_u1.py --host localhost --label u1sim --out tests/fixtures/u1sim --watch-seconds 5 && docker compose -f docker-compose.test.yml down`
Expected: summary with `Q1 API reachable without key: True`; files saved under `tests/fixtures/u1sim/`.

- [ ] **Step 2: Write the contract tests**

Create empty `tests/contract/__init__.py`.

`tests/contract/test_real_u1.py`:

```python
"""The parsers against what the real U1 (probe recordings) and u1sim actually send."""
import json
from pathlib import Path

import pytest

from app.printer import u1
from app.printer.models import PrintState

FIXTURES = Path(__file__).parents[1] / "fixtures"
REAL = FIXTURES / "real_u1"
SIM = FIXTURES / "u1sim"

# (object, field) pairs that app/printer/u1.py reads; None means the object itself.
USED_FIELDS = [
    ("webhooks", "state"), ("print_stats", "state"), ("print_stats", "filename"),
    ("pause_resume", "is_paused"), ("virtual_sdcard", "progress"),
    ("heater_bed", "temperature"), ("extruder", "temperature"), ("extruder3", "temperature"),
    ("exception_manager", None),
    ("print_task_config", "filament_type"), ("print_task_config", "filament_vendor"),
    ("print_task_config", "filament_color_rgba"), ("print_task_config", "filament_exist"),
    ("print_task_config", "extruder_map_table"),
]


def load(folder: Path, name: str):
    path = folder / name
    if not path.exists():
        pytest.skip(f"{path} not recorded yet (see the probe plan)")
    return json.loads(path.read_text())


def objects(folder: Path) -> dict:
    return load(folder, "objects_query.json")["body"]["result"]["status"]


def test_idle_snapshot_parses_as_idle():
    status = u1.parse_status(objects(REAL / "idle"))
    assert status.online and status.klippy_ready and status.idle
    assert len(status.heads) == 4


def test_printing_snapshot_is_busy():
    status = u1.parse_status(objects(REAL / "printing"))
    assert status.state is PrintState.PRINTING and not status.idle


def test_paused_snapshot_is_busy():
    assert not u1.parse_status(objects(REAL / "paused")).idle


def test_complete_snapshot_is_idle():
    assert u1.parse_status(objects(REAL / "complete")).idle


def test_loaded_heads_have_material_and_colour():
    for head in u1.parse_status(objects(REAL / "idle")).heads:
        if head.present:
            assert head.material and head.rgb and len(head.rgb) == 6


def test_real_file_metadata_gives_colours():
    entry = load(REAL / "idle", "file_metadata.json")
    if entry["status"] != 200:
        pytest.skip("no metadata recorded for the probed file")
    tail = (REAL / "idle" / "file_tail.gcode.txt").read_text(errors="replace")
    info = u1.parse_file_info("probe.gcode", entry["body"]["result"], u1.parse_gcode_comments(tail))
    assert info.colours and all(c.rgb for c in info.colours)


def test_history_parses_when_available():
    entry = load(REAL / "complete", "history_list.json")
    if entry["status"] != 200:
        pytest.skip("job history is not available on this firmware")
    assert u1.parse_history(entry["body"]["result"])


def test_head_map_readback_matches_what_was_sent():
    readback = load(REAL / "start_test", "map_readback.json")
    assert u1.head_map_matches(readback["print_task_config"], [(0, 2)])


@pytest.mark.parametrize("obj, field", USED_FIELDS)
def test_real_printer_and_simulator_report_every_field_we_read(obj, field):
    real, sim = objects(REAL / "idle"), objects(SIM)
    assert obj in real, f"real printer lacks {obj}: update app/printer/u1.py"
    assert obj in sim, f"u1sim lacks {obj}"
    if field is not None:
        assert field in real[obj], f"real printer lacks {obj}.{field}: update app/printer/u1.py"
        assert field in sim[obj], f"u1sim lacks {obj}.{field}"
```

- [ ] **Step 3: Run the contract tests**

Run: `pytest tests/contract -v`
Expected: PASS. A failure names the object, field or state to fix. Change the matching `PROBE` constant or parser in `app/printer/u1.py`, add a unit test to `tests/unit/test_u1_parsing.py` that reproduces the real shape, and re-run `pytest -v` until everything passes.

- [ ] **Step 4: Commit**

```bash
git add tests/contract tests/fixtures/u1sim app/printer/u1.py tests/unit/test_u1_parsing.py
git commit -m "test: contract tests against real U1 recordings and u1sim"
```

---

### Task 21: Acceptance on the real printer (🧑)

**Files:**
- Create: `docs/acceptance/v1-acceptance.md`

**Interfaces:**
- Consumes: the deployed app (Task 18), the probe's `probe_square.gcode` (probe plan Task 3).
- Produces: a filled-in acceptance record.

Each step needs a person at the printer. An agent executing this plan stops and hands each step to the user.

- [ ] **Step 1 (🧑): Deploy on the home box**

On the home box: clone the repository, `cp .env.example .env`, set `PRINTER_HOST`, `APP_PASSWORD`, `TZ`, the Telegram variables, `APP_BASE_URL`, and keep `DRY_RUN=true`. Run `docker compose up -d --build`.
Expected: `curl http://localhost:8000/healthz` returns `{"ok":true,"printer_online":true}`.

- [ ] **Step 2 (🧑): Telegram test**

Log in, then press "Send Telegram test message".
Expected: the message arrives in Telegram within a few seconds.

- [ ] **Step 3 (🧑): Dry run**

Upload `probe_square.gcode` on the Files page. Clear the bed, press "Bed is empty and clean", and schedule the file 5 minutes ahead on Head 1.
Expected: at the scheduled time the job shows `skipped: dry run (all checks passed)` and a Telegram message arrives. The printer does not move.

- [ ] **Step 4 (🧑): Real start**

Set `DRY_RUN=false` in `.env` and run `docker compose up -d`. Confirm the bed again and schedule the same file 5 minutes ahead.
Expected: the print starts on Head 1 at the scheduled time. The job shows `started`, "Started …" arrives in Telegram, and "… finished: complete" arrives when it ends.

- [ ] **Step 5 (🧑): Skip without confirmation**

Without confirming the bed, schedule the file 5 minutes ahead.
Expected: a reminder right away (the job is inside the 60-minute window), then `skipped: no confirmation` at the scheduled time, on the page and in Telegram.

- [ ] **Step 6 (🧑): Voided by a touchscreen print**

Confirm the bed and schedule a job 2 hours ahead. Then start any print from the touchscreen.
Expected: "Bed confirmation voided: printed since confirmation. 1 job waiting." on the page and in Telegram. Cancel the scheduled job afterwards.

- [ ] **Step 7 (🧑): Restart keeps a valid confirmation**

Confirm the bed, run `docker compose restart`, and reload the dashboard.
Expected: the bed panel still shows the confirmation as valid, because the restart gap is under 2 minutes.

- [ ] **Step 7b (🧑): A print while the app is down voids the confirmation**

Confirm the bed, then `docker compose stop`. Run a short print from the touchscreen to completion (and clear the bed), then `docker compose start` and reload the dashboard.
Expected: the confirmation shows voided with "printed since confirmation" (found through the printer's job history), on the page and in Telegram.

- [ ] **Step 8: Record and commit**

Create `docs/acceptance/v1-acceptance.md` with a table of Steps 1–7b, each row giving the expected result, what happened, and pass/fail. Then:

```bash
git add docs/acceptance/v1-acceptance.md
git commit -m "docs: v1 acceptance run on the real printer"
```
