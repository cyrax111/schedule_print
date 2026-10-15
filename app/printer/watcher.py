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
        # A start may arrive already paused (e.g. a filament prompt within one status batch).
        # Paused → printing is a resume, not a new start.
        if status.state in ACTIVE and previous.state not in ACTIVE:
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
