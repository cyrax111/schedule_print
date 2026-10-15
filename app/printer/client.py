"""HTTP client for the U1's Moonraker API."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from . import u1
from .models import FileEntry, FileInfo, HistoryJob, PrinterStatus

TAIL_BYTES = 131072
GCODE_SUFFIXES = (".gcode", ".gco", ".g")
# history() asks the printer from this long before `since` (see there); a printer clock further
# off than this can't be checked.
HISTORY_LOOKBACK = timedelta(days=1)


class PrinterError(Exception):
    """The printer answered, but not with what was needed."""


class PrinterOffline(PrinterError):
    """The printer could not be reached."""


def _objects_query(objects: dict[str, list[str] | None]) -> str:
    return "&".join(name if not fields else f"{name}={','.join(fields)}"
                    for name, fields in objects.items())


@contextmanager
def _shaped(what: str) -> Iterator[None]:
    """Turn any malformed or oddly shaped printer answer into a PrinterError."""
    try:
        yield
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise PrinterError(f"unexpected answer from printer ({what})") from exc


def _offline(exc: httpx.TransportError) -> PrinterOffline:
    return PrinterOffline(f"printer unreachable: {exc.__class__.__name__}")


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


class PrinterClient:
    def __init__(self, base_url: str, api_key: str | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        headers = {"X-Api-Key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout,
                                       transport=transport)
        self._info_cache: dict[tuple[str, float], FileInfo] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.DecodingError as exc:
            raise PrinterError("unexpected answer from printer (encoding)") from exc
        except httpx.TransportError as exc:  # connection refused, DNS, timeouts
            raise _offline(exc) from exc

    def _check(self, method: str, path: str, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise PrinterError(
                f"{method} {path} failed: HTTP {response.status_code} {_error_text(response)}".strip())

    async def _result(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        self._check(method, path, response)
        with _shaped(path):
            return response.json()["result"]

    async def query(self, objects: dict[str, list[str] | None]) -> dict[str, Any]:
        result = await self._result("GET", f"/printer/objects/query?{_objects_query(objects)}")
        with _shaped("query"):
            status = result.get("status", {})
            if not isinstance(status, dict):
                raise TypeError("status is not an object")
            return status

    async def status(self) -> PrinterStatus:
        try:
            objects = await self.query(u1.SUBSCRIBE_OBJECTS)
            with _shaped("status"):
                return u1.parse_status(objects)
        except PrinterOffline:
            return PrinterStatus.offline()
        except PrinterError:  # Moonraker up, Klipper not ready
            return PrinterStatus(online=True)

    async def files(self) -> list[FileEntry]:
        result = await self._result("GET", "/server/files/list", params={"root": "gcodes"})
        entries = []
        with _shaped("file list"):
            for item in result:
                name = item.get("path") or item.get("filename")
                if name and name.lower().endswith(GCODE_SUFFIXES):
                    entries.append(FileEntry(name, int(item.get("size") or 0),
                                             float(item.get("modified") or 0)))
        return sorted(entries, key=lambda e: e.modified, reverse=True)

    async def file_info(self, filename: str) -> FileInfo | None:
        path = "/server/files/metadata"
        response = await self._request("GET", path, params={"filename": filename})
        if response.status_code == 404:
            return None
        self._check("GET", path, response)
        with _shaped("metadata"):
            meta = response.json()["result"]
            key = (filename, float(meta.get("modified") or 0))
            needs_tail = key not in self._info_cache and u1.needs_gcode_tail(meta)
        if key not in self._info_cache:
            tail = await self._tail(filename) if needs_tail else ""
            with _shaped("metadata"):
                comments = u1.parse_gcode_comments(tail) if tail else {}
                info = u1.parse_file_info(filename, meta, comments)
            if tail is None:
                return info  # the tail could not be read: ask again next time, don't remember this
            self._info_cache[key] = info
        return self._info_cache[key]

    async def _tail(self, filename: str) -> str | None:
        """Last TAIL_BYTES of the file (None if refused); works whether or not the server honours Range."""
        path = f"/server/files/gcodes/{quote(filename)}"
        try:
            async with self._http.stream("GET", path, headers={"Range": f"bytes=-{TAIL_BYTES}"}) as response:
                if response.status_code >= 400:
                    return None
                tail = bytearray()
                async for chunk in response.aiter_bytes():
                    tail.extend(chunk)
                    if len(tail) > 2 * TAIL_BYTES:
                        del tail[: len(tail) - TAIL_BYTES]
                return bytes(tail[-TAIL_BYTES:]).decode("utf-8", errors="replace")
        except httpx.DecodingError as exc:
            raise PrinterError("unexpected answer from printer (encoding)") from exc
        except httpx.TransportError as exc:
            raise _offline(exc) from exc

    async def upload(self, filename: str, content: bytes) -> None:
        path = "/server/files/upload"
        response = await self._request(
            "POST", path,
            data={"root": "gcodes", "print": "false"},
            files={"file": (filename, content, "application/octet-stream")},
        )
        self._check("POST", path, response)
        with _shaped("upload"):
            try:
                body = response.json()
            except ValueError:
                body = {}
            body = body.get("result", body) if isinstance(body, dict) else {}
            started = isinstance(body, dict) and body.get("print_started")
        if started:
            raise PrinterError("the printer started the uploaded file; it should only store it")

    async def run_gcode(self, script: str) -> None:
        await self._result("POST", "/printer/gcode/script", json={"script": script})

    async def apply_head_map(self, assignments: Sequence[tuple[int, int]]) -> None:
        if not assignments:
            return
        await self.run_gcode(u1.head_map_script(assignments))
        objects = await self.query({"print_task_config": ["extruder_map_table"]})
        with _shaped("head map"):
            matches = u1.head_map_matches(objects.get("print_task_config"), assignments)
        if not matches:
            raise PrinterError("head mapping did not take effect")

    async def start_print(self, filename: str) -> None:
        await self._result("POST", "/printer/print/start", params={"filename": filename})

    async def history(self, since: datetime) -> list[HistoryJob] | None:
        """Jobs started at or after `since` (our clock), or None when history can't be read."""
        # The printer filters by `since` and stamps start times with its own clock, which may be
        # behind ours. The difference is only known from this reply's Date header, so ask from
        # HISTORY_LOOKBACK earlier, newest first (a server ignoring `since` then still returns the
        # newest jobs), move each start time onto our clock and filter here.
        try:
            response = await self._request("GET", "/server/history/list", params={
                "since": (since - HISTORY_LOOKBACK).timestamp(), "order": "desc", "limit": 100})
        except PrinterError:  # offline or undecodable
            return None
        our_now = datetime.now(timezone.utc)
        if response.status_code >= 400:
            return None
        try:
            # Missing or unreadable Date: we can't place the jobs on our clock, so can't verify.
            ahead = parsedate_to_datetime(response.headers["date"]) - our_now
            jobs = u1.parse_history(response.json()["result"])
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return None
        if abs(ahead) > HISTORY_LOOKBACK:
            return None  # the printer's own `since` filter may have dropped jobs we need
        moved = (replace(job, start_time=job.start_time - ahead) for job in jobs)
        return [job for job in moved if job.start_time >= since]

    async def thumbnail(self, path: str) -> bytes | None:
        response = await self._request("GET", f"/server/files/gcodes/{quote(path)}")
        return response.content if response.status_code == 200 else None

    async def camera_snapshot(self) -> bytes | None:
        """The camera's latest picture; None while the camera sleeps (see app/printer/camera.py)."""
        response = await self._request("GET", "/server/files/camera/monitor.jpg")
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        return response.content
