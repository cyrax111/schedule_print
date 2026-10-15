"""Wiring: build every part, run the background tasks, serve the app."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from app.printer.camera import Camera, default_connect, wake_over_websocket
from app.printer.client import PrinterClient
from app.printer.events import Event, EventBus, PrintFinished
from app.printer.watcher import PrinterWatcher
from app.web.app import create_app
from app.web.deps import Services

log = logging.getLogger(__name__)


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
    camera: Camera
    dead_tasks: set[str] = field(default_factory=set)


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
    camera_connect = ws_connect or default_connect(settings.printer_api_key)
    camera = Camera(lambda: wake_over_websocket(camera_connect, settings.printer_ws_url),
                    printer.camera_snapshot, clock)
    return Runtime(settings, clock, conn, printer, bus, watcher, bedcheck, jobs, notifier, runner,
                   sender, delivery, camera)


def create_app_from_runtime(rt: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt.bedcheck.startup()
        def on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            rt.dead_tasks.add(task.get_name())
            log.critical("background task %s stopped", task.get_name(), exc_info=task.exception())

        tasks = [asyncio.create_task(rt.watcher.run(), name="printer-watcher"),
                 asyncio.create_task(rt.runner.run(), name="job-runner")]
        if rt.delivery is not None:
            tasks.append(asyncio.create_task(rt.delivery.run(), name="telegram-delivery"))
        for task in tasks:
            task.add_done_callback(on_done)
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
                   wake_runner=rt.runner.wake, dead_tasks=lambda: rt.dead_tasks, camera=rt.camera)
    return create_app(svc, secret_key=load_or_create_secret(rt.settings), lifespan=lifespan)


def create_app_from_env() -> FastAPI:
    """uvicorn entry point: `uvicorn --factory app.main:create_app_from_env`."""
    configure_logging()
    return create_app_from_runtime(build_runtime(Settings.from_env()))
