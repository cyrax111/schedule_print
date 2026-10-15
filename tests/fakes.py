"""Fakes for printer, websocket and Telegram used across tests."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.printer.models import FileEntry, FileInfo, PrinterStatus


def ok(result, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"result": result})


class Router:
    """httpx.MockTransport handler: routes by (method, decoded path) and records requests."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response | Callable[[httpx.Request], httpx.Response]]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return handler(request) if callable(handler) else handler


class FakeWS:
    """Stands in for a Moonraker websocket connection."""

    def __init__(self, objects: dict | None = None, *, reply: bool = True,
                 answers: dict | None = None, errors: dict | None = None) -> None:
        self.objects = objects
        self.reply = reply
        self.answers = answers or {}   # method -> result, for other calls than subscribe
        self.errors = errors or {}     # method -> JSON-RPC error
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "FakeWS":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        if self.reply and message.get("method") == "printer.objects.subscribe":
            await self.incoming.put({"jsonrpc": "2.0", "id": message["id"],
                                     "result": {"eventtime": 1.0, "status": self.objects}})
        elif message.get("method") in self.answers:
            await self.incoming.put({"jsonrpc": "2.0", "id": message["id"],
                                     "result": self.answers[message["method"]]})
        elif message.get("method") in self.errors:
            await self.incoming.put({"jsonrpc": "2.0", "id": message["id"],
                                     "error": self.errors[message["method"]]})

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def push(self, message: dict) -> None:
        self.incoming.put_nowait(message)

    def push_update(self, objects: dict) -> None:
        self.push({"jsonrpc": "2.0", "method": "notify_status_update", "params": [objects, 2.0]})

    def drop(self) -> None:
        self.incoming.put_nowait(ConnectionError("connection dropped"))


def connector(*sockets: FakeWS):
    """connect() replacement: hands out the given sockets, then ones that never answer."""
    queue = list(sockets)

    def connect(url: str) -> FakeWS:
        return queue.pop(0) if queue else FakeWS(reply=False)

    return connect


async def eventually(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@dataclass
class StatusHolder:
    """Mutable printer status for tests: pass `lambda: holder.status` as the provider."""
    status: PrinterStatus


class FakeHistory:
    def __init__(self, jobs=()) -> None:
        self.jobs = list(jobs)   # set to None to simulate history being unavailable
        self.calls: list = []

    async def history(self, since):
        self.calls.append(since)
        return self.jobs


class FakePrinter:
    """In-memory printer offering the PrinterClient methods the app uses."""

    def __init__(self, files: dict[str, FileInfo] | None = None) -> None:
        self.files_by_name: dict[str, FileInfo] = dict(files or {})
        self.calls: list[tuple] = []
        self.fail_on: dict[str, Exception] = {}
        self.on_start: Callable[[str], None] | None = None
        self.on_file_info: Callable[[str], None] | None = None
        self.uploaded: dict[str, bytes] = {}
        self.history_jobs: list | None = []

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise self.fail_on[method]

    async def files(self) -> list[FileEntry]:
        self._maybe_fail("files")
        return [FileEntry(f.filename, f.size, f.modified) for f in self.files_by_name.values()]

    async def file_info(self, filename: str) -> FileInfo | None:
        self.calls.append(("file_info", filename))
        self._maybe_fail("file_info")
        if self.on_file_info is not None:
            self.on_file_info(filename)
        return self.files_by_name.get(filename)

    async def upload(self, filename: str, content: bytes) -> None:
        self._maybe_fail("upload")
        self.uploaded[filename] = content
        self.files_by_name[filename] = FileInfo(filename, len(content), 1.0)

    async def apply_head_map(self, assignments) -> None:
        self.calls.append(("apply_head_map", list(assignments)))
        self._maybe_fail("apply_head_map")

    async def start_print(self, filename: str) -> None:
        self.calls.append(("start_print", filename))
        self._maybe_fail("start_print")
        if self.on_start is not None:
            self.on_start(filename)

    async def history(self, since):
        return self.history_jobs

    async def thumbnail(self, path: str) -> bytes | None:
        self._maybe_fail("thumbnail")
        return b"PNG"


class FakeCamera:
    """Stands in for app.printer.camera.Camera."""

    def __init__(self, image: bytes | None = b"\xff\xd8JPEG") -> None:
        self.image = image
        self.snapshots = 0

    async def snapshot(self) -> bytes | None:
        self.snapshots += 1
        return self.image
