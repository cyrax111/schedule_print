# U1 Printer Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the eight probe questions in spec §11 against the user's own Snapmaker U1 and save the printer's real responses as test fixtures.

**Architecture:** Two standalone scripts in `tools/`, run with `uv` (dependencies declared inline, PEP 723). `probe_u1.py` is read-only: GET requests and one websocket subscription. `probe_start.py` uploads a small file, sets a head mapping and starts one print, each only after a typed confirmation from a person standing at the printer. Outputs: JSON fixtures under `tests/fixtures/real_u1/` and the findings note `docs/superpowers/probe/u1-findings.md`.

**Tech Stack:** Python 3.12, httpx, websockets, uv.

**Spec:** `docs/superpowers/specs/2026-10-14-schedule-print-design.md` (§2 research, §3 decomposition, §11 probe questions)

**Followed by:** `docs/superpowers/plans/2026-10-14-scheduler-core.md`. Its Task 20 consumes the fixtures recorded here.

## Global Constraints

- `probe_u1.py` sends only GET requests and a `printer.objects.subscribe`; it never sends G-code, uploads, or starts anything.
- `probe_start.py` exits unless run in an interactive terminal, and asks for an exact typed word before every change to the printer.
- 🧑 marks steps that need a person at the printer. An agent executing this plan stops at each 🧑 step and asks the user to do it.
- Saved fixtures never contain the API key (request headers are not saved).
- Do not provoke printer errors to record an error state; record one only if it happens naturally.

## Review Focus

- Wrong IP, printer asleep or powered off → the probe prints a readable error per endpoint, saves what it got (`"status": null`), and exits normally instead of crashing.
- Moonraker requires an API key (HTTP 401) → the summary says so and shows how to rerun with `--api-key`.
- Printer ignores the `Range` header (HTTP 200 instead of 206) → the probe still saves only the last 128 KB of the G-code file.
- Websocket closes before the watch time ends → the frames received so far are still saved.
- The operator types anything other than the exact word at a prompt → `probe_start.py` stops before making that change.

The first item is checked in Task 1 Step 3; the others are handled in the code shown and observed while running Tasks 1–3.

---

## Before you start (🧑)

- [ ] Install uv: `brew install uv` (or see https://docs.astral.sh/uv/).
- [ ] Find the printer's IP address on the touchscreen (Settings → Network), and give the printer a DHCP reservation in your router so the address never changes.
- [ ] Have at least one multi-colour file sliced in Snapmaker Orca already on the printer (any earlier print is fine). Note its exact file name as the printer shows it.

---

### Task 1: Read-only probe tool and idle snapshot

**Files:**
- Create: `tools/probe_u1.py`
- Create (by running): `tests/fixtures/real_u1/idle/*`

**Interfaces:**
- Consumes: nothing.
- Produces: fixture folder layout used by scheduler-core Task 20. For each label: `server_info.json`, `printer_info.json`, `objects_list.json`, `objects_query.json`, `files_roots.json`, `files_list.json`, `history_list.json`, optional `file_metadata.json`, `file_tail.gcode.txt`, `file_tail_response.json`, and `ws_stream.jsonl`. Every `.json` file has the shape `{"status": <int|null>, "body": <json|text>, "error"?: <str>}`.

- [ ] **Step 1: Write the probe script**

Create `tools/probe_u1.py`:

```python
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
    return {"status": response.status_code, "body": body}


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
```

- [ ] **Step 2: Check the script loads**

Run: `uv run tools/probe_u1.py --help`
Expected: usage text listing `--host`, `--label`, `--file`, `--watch-seconds`, `--out`.

- [ ] **Step 3: Check failure handling against an address with no printer**

Run: `uv run tools/probe_u1.py --host 192.0.2.1 --label unreachable --watch-seconds 2 --out /tmp/probe-unreachable`
Expected: one `ConnectError` or `ConnectTimeout` line per endpoint, then a summary with `server/info status None`, `websocket frames recorded: 0`, and exit code 0. Delete `/tmp/probe-unreachable` afterwards.

- [ ] **Step 4 (🧑): Record the idle state**

With the printer switched on, idle, and nothing printing, run (replace the IP and file name):

```bash
uv run tools/probe_u1.py --host 192.168.1.50 --label idle --file "your multi-colour file.gcode"
```

Expected: `Q1 API reachable without key: True`, `Q2 objects missing from query: none` (if some are missing, write them down for Task 4), `websocket frames recorded:` above 0. If Q1 says a key is needed, rerun with `--api-key`.

- [ ] **Step 5: Commit**

```bash
git add tools/probe_u1.py tests/fixtures/real_u1/idle
git commit -m "probe: add read-only U1 probe and idle fixtures"
```

---

### Task 2: Record printing, paused and complete states (🧑)

**Files:**
- Create (by running): `tests/fixtures/real_u1/printing/*`, `tests/fixtures/real_u1/paused/*`, `tests/fixtures/real_u1/complete/*`

**Interfaces:**
- Consumes: `tools/probe_u1.py` from Task 1.
- Produces: fixture folders `printing`, `paused`, `complete` with the layout from Task 1.

- [ ] **Step 1 (🧑): Start any ordinary print from the touchscreen**

Use a short print you wanted anyway. Note the time you pressed start.

- [ ] **Step 2: Record while printing**

Run: `uv run tools/probe_u1.py --host 192.168.1.50 --label printing --watch-seconds 30`
Expected: `print_stats.state='printing'` in the Q3 line.

- [ ] **Step 3 (🧑): Pause from the touchscreen, then record**

Run: `uv run tools/probe_u1.py --host 192.168.1.50 --label paused --watch-seconds 10`
Expected: Q3 shows `'paused'`. Resume the print on the touchscreen afterwards.

- [ ] **Step 4 (🧑): After the print finishes and before starting anything else, record**

Run: `uv run tools/probe_u1.py --host 192.168.1.50 --label complete --watch-seconds 10`
Expected: Q3 shows `'complete'` (write down the actual value if different).

- [ ] **Step 5: Check job history (Q4)**

Run: `python3 -m json.tool tests/fixtures/real_u1/complete/history_list.json | head -40`
Expected, if history is enabled: `"status": 200` and a job whose `start_time` (Unix seconds) matches when you pressed start in Step 1. Record in your notes whether the touchscreen print is listed. If the status is 404, history is not available on this firmware.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/real_u1/printing tests/fixtures/real_u1/paused tests/fixtures/real_u1/complete
git commit -m "probe: record printing, paused and complete fixtures"
```

---

### Task 3: Supervised upload, head mapping and start (🧑)

**Files:**
- Create: `tools/probe_start.py`
- Create (by running): `tests/fixtures/real_u1/start_test/*`

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone script).
- Produces: `tests/fixtures/real_u1/start_test/` with `upload.json`, `after_upload.json`, `map_response.json`, `map_readback.json`, `start_response.json`, `timeline.json`, `answers.json`.

- [ ] **Step 1 (🧑): Prepare a tiny test file**

In Snapmaker Orca, slice a 20 × 20 × 0.6 mm square using **filament 1 only**, and export the G-code to your computer as `probe_square.gcode`. In Step 4 this file will be mapped to **Head 3**, so the print proves that the mapping is honoured.

- [ ] **Step 2: Write the supervised start script**

Create `tools/probe_start.py`:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Supervised start test for a Snapmaker U1 (probe questions 6-8).

Every step that changes the printer asks for a typed word. Run it only while
standing at the printer with an empty, clean bed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

IDLE = {"standby", "complete", "cancelled"}


def ask(prompt: str, word: str) -> None:
    if not sys.stdin.isatty():
        sys.exit("refusing to run: needs an interactive terminal")
    if input(f"{prompt}\nType {word} to continue: ").strip() != word:
        sys.exit("stopped by operator; nothing further was changed")


def yes_no(prompt: str) -> bool:
    return input(f"{prompt} (y/n): ").strip().lower().startswith("y")


def save(out: Path, name: str, data: object) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(data, indent=2, sort_keys=True))


def parse_map(text: str) -> list[tuple[int, int]]:
    pairs = []
    for part in text.split(","):
        logical, _, head = part.partition(":")
        pairs.append((int(logical), int(head)))
    return pairs


async def query(http: httpx.AsyncClient, *objects: str) -> dict:
    response = await http.get("/printer/objects/query?" + "&".join(objects))
    response.raise_for_status()
    return response.json()["result"]["status"]


def record(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:2000]
    return {"status": response.status_code, "body": body}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=7125)
    parser.add_argument("--api-key")
    parser.add_argument("--file", required=True, type=Path, help="local G-code file to upload")
    parser.add_argument("--map", required=True, help="logical:head pairs, heads 0-3, e.g. 0:2")
    parser.add_argument("--observe-seconds", type=int, default=300)
    parser.add_argument("--label", default="start_test")
    args = parser.parse_args()

    out = Path("tests/fixtures/real_u1") / args.label
    name = args.file.name
    pairs = parse_map(args.map)
    headers = {"X-Api-Key": args.api_key} if args.api_key else {}
    async with httpx.AsyncClient(
        base_url=f"http://{args.host}:{args.port}", headers=headers, timeout=15
    ) as http:
        status = await query(http, "print_stats", "print_task_config")
        state = status["print_stats"]["state"]
        print(f"printer state: {state}")
        if state not in IDLE:
            sys.exit(f"printer is not idle ({state}); stopping")
        ptc = status.get("print_task_config", {})
        print("loaded filament:", json.dumps(
            {key: ptc.get(key) for key in ("filament_exist", "filament_type", "filament_color_rgba")}))

        # Q6: upload without starting
        ask(f"Upload {name} to the printer WITHOUT starting it?", "UPLOAD")
        response = await http.post(
            "/server/files/upload",
            data={"root": "gcodes", "print": "false"},
            files={"file": (name, args.file.read_bytes(), "application/octet-stream")},
        )
        save(out, "upload.json", record(response))
        await asyncio.sleep(3)
        after = await query(http, "print_stats")
        save(out, "after_upload.json", after)
        print(f"after upload: state={after['print_stats']['state']} (should be unchanged)")

        # Q7: head mapping
        script = "\n".join(
            f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={logical} MAP_EXTRUDER={head}"
            for logical, head in pairs
        )
        ask(f"Send this head mapping?\n{script}", "MAP")
        response = await http.post("/printer/gcode/script", json={"script": script})
        save(out, "map_response.json", record(response))
        readback = await query(http, "print_task_config")
        save(out, "map_readback.json", readback)
        table = readback["print_task_config"].get("extruder_map_table")
        print(f"extruder_map_table: {table}")

        # Q8: start
        ask("The bed is empty and clean and you are standing at the printer. Start the print?",
            "START")
        started = time.time()
        response = await http.post("/printer/print/start", params={"filename": name})
        save(out, "start_response.json", record(response))
        print(f"start response: HTTP {response.status_code}")
        timeline = []
        for _ in range(args.observe_seconds):
            status = await query(http, "print_stats", "toolhead", "virtual_sdcard",
                                 "exception_manager")
            row = {
                "t": round(time.time() - started, 1),
                "state": status.get("print_stats", {}).get("state"),
                "filename": status.get("print_stats", {}).get("filename"),
                "active_extruder": status.get("toolhead", {}).get("extruder"),
                "progress": status.get("virtual_sdcard", {}).get("progress"),
                "exceptions": status.get("exception_manager", {}).get("exceptions"),
            }
            timeline.append(row)
            print(row)
            await asyncio.sleep(1)
        save(out, "timeline.json", timeline)

    answers = {
        "q8_touch_needed": yes_no("Did the touchscreen show anything that needed a touch "
                                  "before printing began?"),
        "q7_mapped_head_used": yes_no("Did it print with the head you mapped?"),
        "notes": input("Anything else you noticed: "),
    }
    save(out, "answers.json", answers)
    print(f"saved to {out}. To stop the print, use the touchscreen.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Check the refusal paths without a printer**

Run: `echo | uv run tools/probe_start.py --host 192.0.2.1 --file /dev/null --map 0:2`
Expected: exits with an `httpx.ConnectError`/`ConnectTimeout` traceback before any prompt, because the first status query fails. Nothing is sent.

- [ ] **Step 4 (🧑): Run the supervised start at the printer**

Clear and clean the bed, make sure Head 3 has filament loaded, then run:

```bash
uv run tools/probe_start.py --host 192.168.1.50 --file ~/Desktop/probe_square.gcode --map 0:2
```

Type `UPLOAD`, `MAP` and `START` when asked, and watch the touchscreen and which head picks up. Answer the three questions at the end.

Expected: the upload response is `201` and the state is unchanged after upload (Q6). `extruder_map_table[0]` is `2` (Q7). The timeline shows `state` becoming `printing` within a few seconds, and `active_extruder` becoming `extruder2` once printing begins (Q7, Q8).

- [ ] **Step 5: Commit**

```bash
git add tools/probe_start.py tests/fixtures/real_u1/start_test
git commit -m "probe: add supervised start test and its results"
```

---

### Task 4: Findings note and go/no-go decision

**Files:**
- Create: `docs/superpowers/probe/u1-findings.md`

**Interfaces:**
- Consumes: all fixtures from Tasks 1–3.
- Produces: the findings note that scheduler-core Task 3 (`app/printer/u1.py` constants) and Task 20 (contract tests) read.

- [ ] **Step 1: Write the findings note**

Create `docs/superpowers/probe/u1-findings.md` with this structure, filling every Answer and Evidence cell from the fixtures and your notes:

```markdown
# Snapmaker U1 probe findings

Printer firmware: <software_version from idle/printer_info.json>
Probed on: <date>

| # | Question | Answer | Evidence |
|---|---|---|---|
| Q1 | Moonraker on 7125 reachable without API key? | | idle/server_info.json |
| Q2 | All subscribed objects present? Missing ones? | | idle/objects_query.json |
| Q3 | print_stats.state per state (idle/printing/paused/complete); machine_state_manager values | | */objects_query.json |
| Q4 | /server/history/list enabled? Lists touchscreen prints? | | complete/history_list.json |
| Q5 | Metadata fields for per-colour RGB, type, used length; tail Range supported? | | idle/file_metadata.json, idle/file_tail_response.json |
| Q6 | Upload with print=false stores without starting? | | start_test/upload.json, after_upload.json |
| Q7 | SET_PRINT_EXTRUDER_MAP + /printer/print/start honours the map? extruder_map_table shape? | | start_test/map_readback.json, timeline.json, answers.json |
| Q8 | Remote start needed touchscreen interaction? Seconds until state=printing? | | start_test/answers.json, timeline.json |

## Changes needed in the scheduler core plan

- <one line per difference, naming the constant or function in app/printer/u1.py
  and the new value, e.g. "IDLE_STATES: add PrintState.ERROR (state stays 'error'
  after a failed print)">

## Decision

<GO or STOP, with the reason>
```

- [ ] **Step 2: Apply the decision rules**

- **STOP** and go back to brainstorming if Q1 shows the API is unreachable even with a key, or Q8 shows a remote start needs a touch on the screen.
- **STOP and amend scheduler-core Task 4** (`PrinterClient.start_print`) before building, if Q7 shows the mapping is ignored by `/printer/print/start`. Write the call that worked into the findings note.
- **GO** in every other case. List each other difference (Q2, Q3, Q5) under "Changes needed". Scheduler-core Task 3 applies them to `app/printer/u1.py`.
- If Q4 shows history is unavailable, still **GO**. The design already fails closed: after app downtime, confirmations are voided rather than trusted.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/probe/u1-findings.md
git commit -m "probe: record U1 findings and go/no-go decision"
```
