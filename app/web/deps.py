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
    dead_tasks: Callable[[], set[str]] = lambda: set()  # names of background tasks that stopped
    camera: Any = None  # app.printer.camera.Camera, or a test fake; None means no camera

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.tz)


def services(request: Request) -> Services:
    return request.app.state.services


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200) -> Response:
    return request.app.state.templates.TemplateResponse(
        request, name, context or {}, status_code=status_code)
