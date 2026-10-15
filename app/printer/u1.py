"""Everything specific to the Snapmaker U1 and its Moonraker fork.

Constants marked PROBE come from community research and are checked against
the real printer in docs/superpowers/probe/u1-findings.md.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .models import (
    HEAD_COUNT, FileColour, FileInfo, HeadFilament, HistoryJob, PrinterStatus, PrintState,
    Temperature,
)

EXTRUDER_OBJECTS = ("extruder", "extruder1", "extruder2", "extruder3")

# PROBE Q2: objects and fields the watcher subscribes to (None = all fields).
SUBSCRIBE_OBJECTS: dict[str, list[str] | None] = {
    "webhooks": ["state"],
    "print_stats": ["state", "filename", "print_duration", "info"],
    "virtual_sdcard": ["progress"],
    "pause_resume": ["is_paused"],
    "heater_bed": ["temperature", "target"],
    **{name: ["temperature", "target"] for name in EXTRUDER_OBJECTS},
    "exception_manager": None,
    "print_task_config": [
        "filament_type", "filament_vendor", "filament_color_rgba", "filament_exist",
        "extruder_map_table",
    ],
}

# PROBE Q3: print_stats states in which the printer may be confirmed or started.
# If 'error' turns out to persist after a failed print, add PrintState.ERROR here;
# active faults are still caught through exception_manager.
IDLE_STATES = frozenset({PrintState.STANDBY, PrintState.COMPLETE, PrintState.CANCELLED})

# PROBE Q5: metadata keys tried before falling back to G-code comments. The U1 (firmware 1.6.0)
# sends filament_colour, filament_type and filament_used_mm, and no referenced_tools.
META_COLOUR_KEYS = ("filament_colors", "filament_colour")
META_TYPE_KEYS = ("filament_type",)
META_USED_KEY = "filament_used_mm"
COMMENT_COLOUR_KEY = "filament_colour"
COMMENT_TYPE_KEY = "filament_type"
COMMENT_USED_KEY = "filament used [mm]"

_EMPTY = {"", "NONE", "NULL"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.upper() in _EMPTY else text


def _index(seq: Any, i: int) -> Any:
    if isinstance(seq, (list, tuple)) and i < len(seq):
        return seq[i]
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_rgb(value: Any) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    text = text.lstrip("#").upper()
    if len(text) >= 6 and all(c in "0123456789ABCDEF" for c in text[:6]):
        return text[:6]
    return None


def parse_heads(ptc: Mapping[str, Any] | None) -> tuple[HeadFilament, ...]:
    ptc = ptc or {}
    return tuple(
        HeadFilament(
            head=i,
            present=bool(_index(ptc.get("filament_exist"), i)),
            material=_clean(_index(ptc.get("filament_type"), i)),
            vendor=_clean(_index(ptc.get("filament_vendor"), i)),
            rgb=normalise_rgb(_index(ptc.get("filament_color_rgba"), i)),
        )
        for i in range(HEAD_COUNT)
    )


def parse_exception(obj: Mapping[str, Any] | None) -> str | None:
    if not obj:
        return None
    exceptions = obj.get("exceptions")
    if not exceptions:
        return None
    first = exceptions[0] if isinstance(exceptions, list) else exceptions
    if isinstance(first, Mapping):
        for key in ("message", "msg", "description", "code"):
            if first.get(key) not in (None, ""):
                return str(first[key])
    return str(first)


def parse_state(value: Any) -> PrintState:
    try:
        return PrintState(str(value))
    except ValueError:
        return PrintState.UNKNOWN


def _temperature(obj: Mapping[str, Any] | None) -> Temperature:
    obj = obj or {}
    return Temperature(_float(obj.get("temperature")), _float(obj.get("target")))


def _seconds(value: Any) -> float | None:
    number = _float(value) if not isinstance(value, bool) else None
    return number if number is not None and math.isfinite(number) and number >= 0 else None


def _count(value: Any, minimum: int) -> int | None:
    number = _as_int(value) if not isinstance(value, bool) else None
    return number if number is not None and number >= minimum else None


def parse_status(objects: Mapping[str, Mapping[str, Any]]) -> PrinterStatus:
    stats = objects.get("print_stats") or {}
    info = stats.get("info") if isinstance(stats.get("info"), Mapping) else {}
    state = parse_state(stats.get("state"))
    klippy_ready = (objects.get("webhooks") or {}).get("state") == "ready"
    paused = bool((objects.get("pause_resume") or {}).get("is_paused"))
    exception = parse_exception(objects.get("exception_manager"))
    return PrinterStatus(
        online=True,
        klippy_ready=klippy_ready,
        state=state,
        idle=klippy_ready and state in IDLE_STATES and not paused and exception is None,
        filename=_clean(stats.get("filename")),
        progress=_float((objects.get("virtual_sdcard") or {}).get("progress")) or 0.0,
        print_seconds=_seconds(stats.get("print_duration")),
        layer=_count(info.get("current_layer"), 0),
        total_layers=_count(info.get("total_layer"), 1),
        paused=paused,
        exception=exception,
        bed=_temperature(objects.get("heater_bed")),
        nozzles=tuple(_temperature(objects.get(name)) for name in EXTRUDER_OBJECTS),
        heads=parse_heads(objects.get("print_task_config")),
    )


def parse_gcode_comments(text: str) -> dict[str, str]:
    """Collect `; key = value` lines. Later lines win (Orca's config block is last)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(";") and "=" in line:
            key, _, value = line[1:].partition("=")
            found[key.strip()] = value.strip()
    return found


def _split(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value]
    text = str(value).strip()
    if not text:
        return []
    separator = ";" if ";" in text else ","
    return [part.strip() for part in text.split(separator)]


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if mapping.get(key) not in (None, "", []):
            return mapping[key]
    return None


def _referenced_tools(meta: Mapping[str, Any]) -> tuple[int, ...] | None:
    """The tools the metadata lists; None if it lists none or any entry isn't a tool number."""
    tools = meta.get("referenced_tools")
    if not isinstance(tools, list) or not tools:
        return None
    if not all(isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in tools):
        return None
    return tuple(sorted(set(tools)))


def _meta_lengths(meta: Mapping[str, Any]) -> list[Any] | None:
    lengths = meta.get(META_USED_KEY)
    return lengths if isinstance(lengths, list) and lengths else None


def _tools_used(meta: Mapping[str, Any], comments: Mapping[str, str],
                count: int) -> tuple[int, ...] | None:
    """Tools the file uses, from referenced tools or used lengths; None if nothing is known.

    A filament counts as unused only when its length is exactly zero. A missing, unreadable or
    odd length (nan, negative) counts as used, so that tool is still checked.
    """
    referenced = _referenced_tools(meta)
    if referenced is not None:
        return referenced
    lengths = _meta_lengths(meta) or _split(comments.get(COMMENT_USED_KEY))
    if not lengths:
        return None
    padded = list(lengths) + [None] * (count - len(lengths))
    return tuple(i for i, length in enumerate(padded) if _float(length) != 0)


def parse_file_colours(meta: Mapping[str, Any], comments: Mapping[str, str]) -> tuple[FileColour, ...]:
    colours = _split(_first(meta, META_COLOUR_KEYS) or comments.get(COMMENT_COLOUR_KEY))
    types = _split(_first(meta, META_TYPE_KEYS) or comments.get(COMMENT_TYPE_KEY))
    count = max(len(colours), len(types))
    used = _tools_used(meta, comments, count)

    def colour(i: int) -> FileColour:
        return FileColour(
            i,
            normalise_rgb(colours[i]) if i < len(colours) else None,
            _clean(types[i]) if i < len(types) else None,
        )

    if count == 0:
        # No colour information: one unknown colour per tool used, so every tool gets a head that
        # is checked at start. Nothing known about the tools: none, and scheduling refuses.
        return tuple(colour(i) for i in used or ())
    # A used tool past the end of the colour list keeps an unknown colour, so its head is checked too.
    # Nothing known, or nothing reported as used: check every listed colour.
    return tuple(colour(i) for i in (used or range(count)))


def needs_gcode_tail(meta: Mapping[str, Any]) -> bool:
    """True unless the metadata alone says each used filament's colour and type."""
    return (_first(meta, META_COLOUR_KEYS) is None or _first(meta, META_TYPE_KEYS) is None
            or (_referenced_tools(meta) is None and _meta_lengths(meta) is None))


def parse_file_info(filename: str, meta: Mapping[str, Any], comments: Mapping[str, str]) -> FileInfo:
    thumbnail = None
    thumbs = meta.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        relative = best.get("relative_path")
        if relative:
            folder = filename.rpartition("/")[0]
            thumbnail = f"{folder}/{relative}" if folder else relative
    return FileInfo(
        filename=filename,
        size=int(meta.get("size") or 0),
        modified=float(meta.get("modified") or 0.0),
        estimated_seconds=_float(meta.get("estimated_time")),
        colours=parse_file_colours(meta, comments),
        thumbnail=thumbnail,
    )


def head_map_script(assignments: Iterable[tuple[int, int]]) -> str:
    return "\n".join(
        f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={logical} MAP_EXTRUDER={head}"
        for logical, head in assignments
    )


def head_map_matches(ptc: Mapping[str, Any] | None, assignments: Iterable[tuple[int, int]]) -> bool:
    # PROBE Q7: extruder_map_table is a list indexed by logical colour holding the head.
    table = (ptc or {}).get("extruder_map_table")
    if not isinstance(table, list):
        return False
    return all(logical < len(table) and _as_int(table[logical]) == head
               for logical, head in assignments)


def parse_history(result: Mapping[str, Any]) -> list[HistoryJob]:
    items = result.get("jobs") if isinstance(result, Mapping) else None
    if not isinstance(items, list):  # an answer without a jobs list is not "no prints"
        raise ValueError("history answer without a jobs list")
    jobs = []
    for item in items:
        start = _float(item.get("start_time"))
        if start is None:
            raise ValueError("history entry without a start time")
        jobs.append(HistoryJob(
            filename=item.get("filename"),
            start_time=datetime.fromtimestamp(start, timezone.utc),
            status=str(item.get("status", "")),
        ))
    return jobs
