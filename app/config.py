"""Settings read from environment variables."""
from __future__ import annotations

import os
import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_SECRET_LENGTH = 32


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
    # Secrets are left out of repr, so logging or printing settings never shows them.
    printer_host: str
    app_password: str = field(repr=False)
    printer_port: int = 7125
    printer_api_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    tz: str = "UTC"
    dry_run: bool = False
    offline_grace_minutes: int = 10
    start_verify_seconds: int = 90
    reminder_minutes: int = 60
    data_dir: Path = Path("/data")
    telegram_bot_token: str | None = field(default=None, repr=False)
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
    if settings.secret_key and settings.secret_key.strip():
        return settings.secret_key
    path = settings.data_dir / "secret_key"
    if path.exists():
        # An empty, short or zero-filled file (e.g. after a power cut) is treated as missing:
        # sessions signed with such a key could be forged.
        stored = path.read_text().strip(string.whitespace + "\x00")
        if len(stored) >= MIN_SECRET_LENGTH:
            return stored
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    _write_private(path, secret)
    return secret


def _write_private(path: Path, text: str) -> None:
    """Replace `path` atomically with a file only we can read: a crash leaves the old or the new one."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
