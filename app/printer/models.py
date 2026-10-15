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
    print_seconds: float | None = None   # time spent printing this job, pauses left out
    layer: int | None = None
    total_layers: int | None = None
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
