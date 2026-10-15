"""What the printer panel shows about a running print: how far, how long, when done."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.printer.client import PrinterError
from app.printer.models import PrinterStatus, PrintState

# Below this, a guess from the progress alone swings too much to be worth showing.
MIN_PROGRESS_FOR_GUESS = 0.05


@dataclass(frozen=True)
class ProgressView:
    percent: int
    layer: int | None
    total_layers: int | None
    elapsed: float | None        # seconds spent printing
    left: float | None           # seconds still to go
    finish_at: datetime | None   # only while printing: a paused print has no finish time


def describe_progress(status: PrinterStatus, estimated_seconds: float | None,
                      now: datetime) -> ProgressView | None:
    if status.state not in (PrintState.PRINTING, PrintState.PAUSED):
        return None
    progress = min(max(status.progress, 0.0), 1.0)
    elapsed = status.print_seconds
    left = None
    if elapsed is not None:
        if estimated_seconds and estimated_seconds > elapsed:
            left = estimated_seconds - elapsed   # the slicer's estimate
        elif progress >= MIN_PROGRESS_FOR_GUESS:
            left = elapsed / progress - elapsed  # the rest at the pace so far
    running = status.state is PrintState.PRINTING and not status.paused
    return ProgressView(
        percent=round(progress * 100), layer=status.layer, total_layers=status.total_layers,
        elapsed=elapsed, left=left,
        finish_at=now + timedelta(seconds=left) if left is not None and running else None,
    )


def format_span(seconds: float) -> str:
    minutes = round(seconds / 60)
    if seconds < 60 or minutes == 0:
        return "under a minute"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def format_finish(moment: datetime, now: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{local:%H:%M}" if local.date() == now.astimezone(tz).date() else f"{local:%a %H:%M}"


class FileEstimates:
    """The slicer's time estimate for the file being printed, read from the printer once."""

    def __init__(self, printer: Any) -> None:
        self._printer = printer
        self._last: tuple[str, float | None] | None = None   # only the latest file is kept

    async def seconds(self, filename: str | None) -> float | None:
        if not filename:
            return None
        if self._last is not None and self._last[0] == filename:
            return self._last[1]
        try:
            info = await self._printer.file_info(filename)
        except PrinterError:
            return None   # asked again on the next refresh
        estimate = info.estimated_seconds if info is not None else None
        self._last = (filename, estimate)
        return estimate

    def forget(self) -> None:
        """Called between prints, so a re-sliced file of the same name is read again."""
        self._last = None
