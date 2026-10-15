# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "websockets>=14"]
# ///
"""Read-only probe of a Snapmaker U1's Moonraker API.

Saves raw responses under tests/fixtures/real_u1/<label>/ (or --out) so the
scheduler's parsers can be tested against the real printer. It sends only GET
requests and one websocket subscription: no G-code, no uploads, no starts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

OBJECTS = [
    "webhooks", "print_stats", "virtual_sdcard", "pause_resume", "heater_bed",
    "toolhead", "extruder", "extruder1", "extruder2", "extruder3",
    "exception_manager", "print_task_config", "filament_detect",
    "machine_state_manager",
]
TAIL_BYTES = 131072


def save(out: Path, name: str, data: object) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(data, indent=2, sort_keys=True))


async def fetch(http: httpx.AsyncClient, path: str) -> dict:
    try:
        response = await http.get(path)
    except httpx.HTTPError as exc:
        print(f"  {path}: {exc.__class__.__name__}: {exc}")
        return {"status": None, "body": None, "error": f"{exc.__class__.__name__}: {exc}"}
    try:
        body = response.json()
    except ValueError:
        body = response.text[:2000]
    # The scheduler corrects for the printer's clock using the Date header, so record it
    # alongside this computer's clock at the moment the reply arrived.
    return {"status": response.status_code, "body": body,
            "date": response.headers.get("date"), "received_at": time.time()}


async def probe_http(http: httpx.AsyncClient, out: Path, gcode_file: str | None) -> dict:
    results: dict = {}
    for name, path in [
        ("server_info", "/server/info"),
        ("printer_info", "/printer/info"),
        ("objects_list", "/printer/objects/list"),
        ("objects_query", "/printer/objects/query?" + "&".join(OBJECTS)),
        ("files_roots", "/server/files/roots"),
        ("files_list", "/server/files/list?root=gcodes"),
        ("history_list", "/server/history/list?limit=10&order=desc"),
    ]:
        results[name] = await fetch(http, path)
        save(out, f"{name}.json", results[name])
    if gcode_file:
        encoded = quote(gcode_file)
        results["file_metadata"] = await fetch(http, f"/server/files/metadata?filename={encoded}")
        save(out, "file_metadata.json", results["file_metadata"])
        try:
            response = await http.get(
                f"/server/files/gcodes/{encoded}",
                headers={"Range": f"bytes=-{TAIL_BYTES}"},
            )
            (out / "file_tail.gcode.txt").write_bytes(response.content[-TAIL_BYTES:])
            results["file_tail_status"] = response.status_code
            save(out, "file_tail_response.json", {
                "status": response.status_code,
                "content_range": response.headers.get("content-range"),
                "bytes_received": len(response.content),
            })
        except httpx.HTTPError as exc:
            results["file_tail_status"] = None
            print(f"  tail fetch failed: {exc}")
    return results


async def probe_ws(url: str, api_key: str | None, seconds: float, out: Path) -> list:
    frames: list = []
    headers = {"X-Api-Key": api_key} if api_key else None
    try:
        async with websockets.connect(
            url, max_size=None, open_timeout=10, additional_headers=headers
        ) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "method": "printer.objects.subscribe",
                "params": {"objects": {name: None for name in OBJECTS}}, "id": 1,
            }))
            deadline = time.monotonic() + seconds
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                frames.append({"t": time.time(), "msg": json.loads(raw)})
    except Exception as exc:  # report and keep whatever arrived
        print(f"  websocket: {exc.__class__.__name__}: {exc}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ws_stream.jsonl").write_text("\n".join(json.dumps(frame) for frame in frames))
    return frames


def _result(entry: dict) -> dict:
    body = entry.get("body")
    return body.get("result", {}) if isinstance(body, dict) else {}


def clock_line(entry: dict) -> str:
    date = entry.get("date")
    if entry.get("status") is None:
        return "   clock: no reply"
    if not date:
        return ("   clock: NO Date header; the scheduler can't place history on its own clock, "
                "so any gap in watching would void a confirmation")
    try:
        printer_now = parsedate_to_datetime(date).timestamp()
    except (TypeError, ValueError):
        return f"   clock: unreadable Date header {date!r}"
    return (f"   clock: Date header {date!r}; printer is {printer_now - entry['received_at']:+.0f} s "
            "from this computer (±1 s)")


def summarize(results: dict, frames: list) -> str:
    server = results["server_info"]
    lines = []
    if server["status"] == 401:
        lines.append("Q1 API needs a key (HTTP 401). Rerun with --api-key <key> "
                     "(get one from Fluidd: Settings → Authorization).")
    else:
        lines.append(f"Q1 API reachable without key: {server['status'] == 200} "
                     f"(server/info status {server['status']})")
    info = _result(results["printer_info"])
    lines.append(f"   software_version={info.get('software_version')} state={info.get('state')}")
    status = _result(results["objects_query"]).get("status", {})
    missing = [name for name in OBJECTS if name not in status]
    lines.append(f"Q2 objects missing from query: {missing or 'none'}")
    lines.append(f"Q3 print_stats.state={status.get('print_stats', {}).get('state')!r} "
                 f"machine_state_manager={status.get('machine_state_manager')!r}")
    lines.append(f"Q4 history endpoint status: {results['history_list']['status']}")
    lines.append(clock_line(results["history_list"]))
    if "file_metadata" in results:
        keys = sorted(_result(results["file_metadata"]).keys())
        lines.append(f"Q5 metadata keys: {keys}")
        lines.append(f"   tail fetch status: {results.get('file_tail_status')} (206 = Range supported)")
    lines.append(f"websocket frames recorded: {len(frames)}")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=7125)
    parser.add_argument("--api-key")
    parser.add_argument("--label", required=True, help="state name, e.g. idle, printing")
    parser.add_argument("--file", help="G-code file name on the printer to inspect")
    parser.add_argument("--watch-seconds", type=float, default=20.0)
    parser.add_argument("--out", type=Path, help="output folder (default tests/fixtures/real_u1/<label>)")
    args = parser.parse_args()

    out = args.out or Path("tests/fixtures/real_u1") / args.label
    headers = {"X-Api-Key": args.api_key} if args.api_key else {}
    async with httpx.AsyncClient(
        base_url=f"http://{args.host}:{args.port}", headers=headers, timeout=10
    ) as http:
        results = await probe_http(http, out, args.file)
    frames = await probe_ws(
        f"ws://{args.host}:{args.port}/websocket", args.api_key, args.watch_seconds, out
    )
    print(summarize(results, frames))
    print(f"saved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
