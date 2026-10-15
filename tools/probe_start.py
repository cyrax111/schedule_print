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
        if not 200 <= response.status_code < 300:
            sys.exit(f"upload failed (HTTP {response.status_code}); nothing further was changed")
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
        if not 200 <= response.status_code < 300:
            sys.exit(f"head mapping failed (HTTP {response.status_code}); the print was not started")
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
        try:
            for _ in range(args.observe_seconds):
                t = round(time.time() - started, 1)
                try:
                    status = await query(http, "print_stats", "toolhead", "virtual_sdcard",
                                         "exception_manager")
                    row = {
                        "t": t,
                        "state": status.get("print_stats", {}).get("state"),
                        "filename": status.get("print_stats", {}).get("filename"),
                        "active_extruder": status.get("toolhead", {}).get("extruder"),
                        "progress": status.get("virtual_sdcard", {}).get("progress"),
                        "exceptions": status.get("exception_manager", {}).get("exceptions"),
                    }
                except (httpx.HTTPError, KeyError) as exc:
                    row = {"t": t, "error": f"{exc.__class__.__name__}: {exc}"}
                timeline.append(row)
                print(row)
                await asyncio.sleep(1)
        finally:
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
