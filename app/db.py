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
