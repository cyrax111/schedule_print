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
