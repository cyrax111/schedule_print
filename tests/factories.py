"""Small builders for test data."""
from datetime import datetime, timezone

from app.jobs.models import HeadAssignment, Job, JobStatus
from app.printer.models import (
    FileColour, FileInfo, HeadFilament, PrinterStatus, PrintState, Temperature,
)

IDLE_OBJECTS = {
    "webhooks": {"state": "ready"},
    "print_stats": {"state": "standby", "filename": ""},
    "virtual_sdcard": {"progress": 0.0},
    "pause_resume": {"is_paused": False},
    "heater_bed": {"temperature": 24.5, "target": 0.0},
    "extruder": {"temperature": 25.0, "target": 0.0},
    "extruder1": {"temperature": 25.1, "target": 0.0},
    "extruder2": {"temperature": 25.2, "target": 0.0},
    "extruder3": {"temperature": 25.3, "target": 0.0},
    "exception_manager": {"exceptions": []},
    "print_task_config": {
        "filament_exist": [True, True, True, False],
        "filament_type": ["PLA", "PETG", "PLA", "NONE"],
        "filament_vendor": ["Snapmaker", "Generic", "Polymaker", "NONE"],
        "filament_color_rgba": ["FFFFFFFF", "000000FF", "f78e0eff", "NONE"],
        "extruder_map_table": [0, 1, 2, 3] + [0] * 28,
    },
}


def with_changes(**objects) -> dict:
    merged = {name: dict(fields) for name, fields in IDLE_OBJECTS.items()}
    for name, fields in objects.items():
        merged.setdefault(name, {}).update(fields)
    return merged


DEFAULT_HEADS = (
    HeadFilament(0, True, "PLA", "Snapmaker", "FFFFFF"),
    HeadFilament(1, True, "PLA", "Snapmaker", "000000"),
    HeadFilament(2, True, "PETG", "Generic", "FF0000"),
    HeadFilament(3, False),
)


def make_status(**overrides) -> PrinterStatus:
    values = dict(
        online=True, klippy_ready=True, state=PrintState.STANDBY, idle=True,
        bed=Temperature(25.0, 0.0), nozzles=(Temperature(25.0, 0.0),) * 4, heads=DEFAULT_HEADS,
    )
    values.update(overrides)
    return PrinterStatus(**values)


def printing_status(filename: str = "cube.gcode") -> PrinterStatus:
    return make_status(state=PrintState.PRINTING, idle=False, filename=filename, progress=0.1)


def make_file(filename: str = "cube.gcode", **overrides) -> FileInfo:
    values = dict(
        filename=filename, size=1000, modified=1_760_000_000.0, estimated_seconds=3600.0,
        colours=(FileColour(0, "FFFFFF", "PLA"),), thumbnail=None,
    )
    values.update(overrides)
    return FileInfo(**values)


def make_job(**overrides) -> Job:
    when = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    values = dict(
        id=1, filename="cube.gcode", file_size=1000, file_modified=1_760_000_000.0,
        scheduled_at=when, head_map=(HeadAssignment(0, 2, "PETG", "FF0000"),),
        status=JobStatus.PENDING, outcome_reason=None, reminder_sent=False,
        created_at=when, decided_at=None,
    )
    values.update(overrides)
    return Job(**values)
