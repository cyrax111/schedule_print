import stat
from pathlib import Path

import pytest

from app import config
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


def test_repr_hides_secrets():
    secrets = {"APP_PASSWORD": "hunter2-password", "PRINTER_API_KEY": "api-key-123",
               "TELEGRAM_BOT_TOKEN": "123456:telegram-token", "SECRET_KEY": "session-signing-key"}
    text = repr(Settings.from_env({**BASE, **secrets, "TELEGRAM_CHAT_ID": "42"}))
    assert [value for value in secrets.values() if value in text] == []
    assert "192.168.1.50" in text and "42" in text  # the rest is still shown


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secret_is_created_once(tmp_path):
    s = Settings.from_env({**BASE, "DATA_DIR": str(tmp_path)})
    first = load_or_create_secret(s)
    assert len(first) >= 32
    assert file_mode(tmp_path / "secret_key") == 0o600
    assert load_or_create_secret(s) == first
    assert load_or_create_secret(Settings.from_env({**BASE, "SECRET_KEY": "given"})) == "given"


# An empty or short key file (e.g. after a power cut) must not become the signing key: a cookie
# signed with "" is accepted by itsdangerous.
@pytest.mark.parametrize("stored", ["", "\n", "too-short-key", "\x00" * 43])
def test_unusable_secret_file_is_replaced(tmp_path, stored):
    path = tmp_path / "secret_key"
    path.write_text(stored)
    path.chmod(0o644)
    s = Settings.from_env({**BASE, "DATA_DIR": str(tmp_path)})
    secret = load_or_create_secret(s)
    assert len(secret) >= 32 and path.read_text() == secret
    assert file_mode(path) == 0o600
    assert load_or_create_secret(s) == secret
    assert [p.name for p in tmp_path.iterdir()] == ["secret_key"]  # no temp file left behind


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_secret_key_setting_counts_as_unset(tmp_path, value):
    s = Settings.from_env({**BASE, "DATA_DIR": str(tmp_path), "SECRET_KEY": value})
    secret = load_or_create_secret(s)
    assert len(secret) >= 32 and (tmp_path / "secret_key").read_text() == secret


def test_secret_write_that_fails_leaves_no_key_file(tmp_path, monkeypatch):
    def crash(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(config.os, "replace", crash)
    with pytest.raises(OSError, match="power cut"):
        load_or_create_secret(Settings.from_env({**BASE, "DATA_DIR": str(tmp_path)}))
    assert list(tmp_path.iterdir()) == []  # neither a half-written key nor a temp file


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_password_is_refused(value):
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        Settings.from_env({**BASE, "APP_PASSWORD": value})
