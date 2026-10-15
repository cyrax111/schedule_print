"""The U1's camera: a snapshot the printer refreshes while it is kept awake.

The stock firmware has no video stream. After `camera.start_monitor` the printer writes a fresh
picture to monitor.jpg about every 1.5 s, and stops some seconds after the last such call. So the
camera is woken only while someone looks at it, at most once every WAKE_EVERY seconds.
Nothing here touches printing or the bed check.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import websockets

from app.clock import Clock

log = logging.getLogger(__name__)

WAKE_REQUEST = {"jsonrpc": "2.0", "method": "camera.start_monitor",
                "params": {"domain": "lan", "interval": 0}}
WAKE_EVERY = 8.0          # seconds; the printer keeps the camera on for about 10 s after a wake
WAKE_TIMEOUT = 10.0
WARMUP_TRIES = 3          # extra reads, a second apart, after waking a sleeping camera
WARMUP_PAUSE = 1.0


class CameraError(Exception):
    pass


def default_connect(api_key: str | None = None) -> Callable[[str], Any]:
    headers = {"X-Api-Key": api_key} if api_key else None
    return lambda url: websockets.connect(url, open_timeout=WAKE_TIMEOUT,
                                          additional_headers=headers)


async def wake_over_websocket(connect: Callable[[str], Any], url: str, *,
                              timeout: float = WAKE_TIMEOUT) -> None:
    """Send camera.start_monitor on a short-lived connection and check the printer agreed."""
    async def call() -> dict:
        async with connect(url) as ws:
            await ws.send(json.dumps({**WAKE_REQUEST, "id": 1}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 1:
                    return message

    try:
        message = await asyncio.wait_for(call(), timeout=timeout)
    except TimeoutError:
        raise CameraError("no reply from the printer") from None
    if "error" in message:
        error = message["error"]
        detail = error.get("message") if isinstance(error, dict) else error
        raise CameraError(f"camera.start_monitor failed: {detail}")
    result = message.get("result")
    if not isinstance(result, dict) or result.get("state") != "success":
        raise CameraError(f"camera.start_monitor refused: {result!r}")


class Camera:
    def __init__(self, wake: Callable[[], Awaitable[None]],
                 read: Callable[[], Awaitable[bytes | None]], clock: Clock) -> None:
        self._wake = wake
        self._read = read
        self._clock = clock
        self._last_wake: datetime | None = None
        self._lock = asyncio.Lock()

    async def snapshot(self) -> bytes | None:
        """The latest picture, or None when the camera or printer doesn't give one."""
        async with self._lock:   # several viewers share one wake
            woke = await self._wake_if_due()
            image = await self._try_read()
            for _ in range(WARMUP_TRIES if woke else 0):
                if image is not None:
                    break
                await self._clock.sleep(WARMUP_PAUSE)
                image = await self._try_read()
            return image

    async def _wake_if_due(self) -> bool:
        now = self._clock.now()
        if self._last_wake is not None and (now - self._last_wake).total_seconds() < WAKE_EVERY:
            return False
        self._last_wake = now   # also after a failure, so an offline printer isn't hammered
        try:
            await self._wake()
        except Exception as exc:  # the picture may still be there; a camera fault stays here
            log.warning("could not wake the camera: %s", exc)
        return True

    async def _try_read(self) -> bytes | None:
        try:
            return await self._read()
        except Exception as exc:
            log.warning("could not read the camera picture: %s", exc)
            return None
