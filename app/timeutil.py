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
