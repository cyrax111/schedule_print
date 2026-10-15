"""The parsers against what the real U1 (probe recordings) and u1sim actually send."""
import json
from pathlib import Path

import pytest

from app.printer import u1
from app.printer.models import PrintState

FIXTURES = Path(__file__).parents[1] / "fixtures"
REAL = FIXTURES / "real_u1"
SIM = FIXTURES / "u1sim"

# (object, field) pairs that app/printer/u1.py reads; None means the object itself.
USED_FIELDS = [
    ("webhooks", "state"), ("print_stats", "state"), ("print_stats", "filename"),
    ("print_stats", "print_duration"), ("print_stats", "info"),
    ("pause_resume", "is_paused"), ("virtual_sdcard", "progress"),
    ("heater_bed", "temperature"), ("extruder", "temperature"), ("extruder3", "temperature"),
    ("exception_manager", None),
    ("print_task_config", "filament_type"), ("print_task_config", "filament_vendor"),
    ("print_task_config", "filament_color_rgba"), ("print_task_config", "filament_exist"),
    ("print_task_config", "extruder_map_table"),
]


def load(folder: Path, name: str):
    path = folder / name
    if not path.exists():
        pytest.skip(f"{path} not recorded yet (see the probe plan)")
    return json.loads(path.read_text())


def objects(folder: Path) -> dict:
    return load(folder, "objects_query.json")["body"]["result"]["status"]


def test_idle_snapshot_parses_as_idle():
    status = u1.parse_status(objects(REAL / "idle"))
    assert status.online and status.klippy_ready and status.idle
    assert len(status.heads) == 4


def test_printing_snapshot_is_busy():
    status = u1.parse_status(objects(REAL / "printing"))
    assert status.state is PrintState.PRINTING and not status.idle


def test_printing_snapshot_has_layers_and_print_time():
    status = u1.parse_status(objects(REAL / "printing"))
    assert (status.layer, status.total_layers) == (1, 37)
    assert status.print_seconds == pytest.approx(42.7, abs=0.1)


def test_paused_snapshot_is_busy():
    assert not u1.parse_status(objects(REAL / "paused")).idle


def test_complete_snapshot_is_idle():
    assert u1.parse_status(objects(REAL / "complete")).idle


def test_loaded_heads_have_material_and_colour():
    for head in u1.parse_status(objects(REAL / "idle")).heads:
        if head.present:
            assert head.material and head.rgb and len(head.rgb) == 6


def test_real_file_metadata_gives_colours():
    entry = load(REAL / "idle", "file_metadata.json")
    if entry["status"] != 200:
        pytest.skip("no metadata recorded for the probed file")
    tail_path = REAL / "idle" / "file_tail.gcode.txt"
    if not tail_path.exists():
        pytest.skip("no G-code tail recorded")
    tail = tail_path.read_text(errors="replace")
    info = u1.parse_file_info("probe.gcode", entry["body"]["result"], u1.parse_gcode_comments(tail))
    assert info.colours and all(c.rgb for c in info.colours)


def test_real_metadata_alone_gives_the_same_colours_as_the_gcode_tail():
    entry = load(REAL / "idle", "file_metadata.json")
    tail_path = REAL / "idle" / "file_tail.gcode.txt"
    if entry["status"] != 200 or not tail_path.exists():
        pytest.skip("no metadata or G-code tail recorded")
    meta = entry["body"]["result"]
    tail = u1.parse_gcode_comments(tail_path.read_text(errors="replace"))
    assert not u1.needs_gcode_tail(meta)
    assert u1.parse_file_colours(meta, {}) == u1.parse_file_colours({}, tail)


def test_history_parses_when_available():
    entry = load(REAL / "complete", "history_list.json")
    if entry["status"] != 200:
        pytest.skip("job history is not available on this firmware")
    assert u1.parse_history(entry["body"]["result"])


def test_head_map_readback_matches_what_was_sent():
    readback = load(REAL / "start_test", "map_readback.json")
    assert u1.head_map_matches(readback["print_task_config"], [(0, 2)])


@pytest.mark.parametrize("obj, field", USED_FIELDS)
def test_real_printer_and_simulator_report_every_field_we_read(obj, field):
    real, sim = objects(REAL / "idle"), objects(SIM)
    assert obj in real, f"real printer lacks {obj}: update app/printer/u1.py"
    assert obj in sim, f"u1sim lacks {obj}"
    if field is not None:
        assert field in real[obj], f"real printer lacks {obj}.{field}: update app/printer/u1.py"
        assert field in sim[obj], f"u1sim lacks {obj}.{field}"
