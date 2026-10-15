from datetime import datetime, timedelta, timezone

import pytest

from app.printer import u1
from app.printer.models import FileColour, HistoryJob, PrintState
from tests.factories import IDLE_OBJECTS, with_changes


def test_idle_printer():
    status = u1.parse_status(IDLE_OBJECTS)
    assert status.online and status.klippy_ready and status.idle
    assert status.state is PrintState.STANDBY
    assert status.filename is None
    assert [h.rgb for h in status.heads] == ["FFFFFF", "000000", "F78E0E", None]
    assert [h.present for h in status.heads] == [True, True, True, False]
    assert status.heads[3].material is None
    assert status.nozzles[2].current == 25.2
    assert status.bed.current == 24.5


def test_printing_is_not_idle():
    status = u1.parse_status(with_changes(print_stats={"state": "printing", "filename": "cube.gcode"}))
    assert status.state is PrintState.PRINTING and not status.idle
    assert status.filename == "cube.gcode"


def test_paused_flag_is_not_idle():
    assert not u1.parse_status(with_changes(pause_resume={"is_paused": True})).idle


def test_active_exception_is_not_idle():
    status = u1.parse_status(with_changes(
        exception_manager={"exceptions": [{"code": 3, "message": "Nozzle clog"}]}))
    assert status.exception == "Nozzle clog" and not status.idle


def test_klippy_not_ready_is_not_idle():
    assert not u1.parse_status(with_changes(webhooks={"state": "startup"})).idle


def test_unknown_state_is_not_idle():
    status = u1.parse_status(with_changes(print_stats={"state": "calibrating"}))
    assert status.state is PrintState.UNKNOWN and not status.idle


def test_printing_progress_fields():
    status = u1.parse_status(with_changes(print_stats={
        "state": "printing", "print_duration": 42.7, "info": {"current_layer": 1, "total_layer": 37}}))
    assert (status.print_seconds, status.layer, status.total_layers) == (42.7, 1, 37)


@pytest.mark.parametrize("info", [None, {}, "x", {"current_layer": None, "total_layer": 0},
                                  {"current_layer": "?", "total_layer": -3}])
def test_missing_or_odd_layer_info_is_unknown(info):
    status = u1.parse_status(with_changes(print_stats={"state": "printing", "info": info}))
    assert (status.print_seconds, status.layer, status.total_layers) == (None, None, None)


@pytest.mark.parametrize("value", ["nan", float("inf"), -5.0, "?", True])
def test_odd_print_time_is_unknown(value):
    status = u1.parse_status(with_changes(print_stats={"state": "printing", "print_duration": value}))
    assert status.print_seconds is None


def test_half_reported_heads_do_not_crash():  # Review Focus: half-reported head slots
    status = u1.parse_status(with_changes(print_task_config={
        "filament_exist": [True],
        "filament_type": ["", "NONE"],
        "filament_color_rgba": ["12", None],
        "filament_vendor": None,
    }))
    assert len(status.heads) == 4
    first = status.heads[0]
    assert first.present and first.material is None and first.rgb is None
    assert not any(h.present for h in status.heads[1:])


def test_missing_task_config_gives_four_empty_heads():
    objects = with_changes()
    del objects["print_task_config"]
    assert [h.present for h in u1.parse_status(objects).heads] == [False] * 4


def test_colours_from_metadata_lists():
    meta = {"filament_colors": ["#FF0000", "#00FF00", "#0000FF"],
            "filament_type": "PLA;PETG;PLA", "referenced_tools": [0, 2]}
    assert u1.parse_file_colours(meta, {}) == (
        FileColour(0, "FF0000", "PLA"), FileColour(2, "0000FF", "PLA"))


def test_colours_from_gcode_comments_skip_unused_filaments():
    comments = u1.parse_gcode_comments(
        "; filament used [mm] = 120.50, 0.00, 30.2\n"
        "; filament_colour = #FFFFFF;#000000;#ff0000\n"
        "; filament_type = PLA;PLA;PETG\n"
    )
    assert u1.parse_file_colours({}, comments) == (
        FileColour(0, "FFFFFF", "PLA"), FileColour(2, "FF0000", "PETG"))


def test_file_without_colour_or_tool_information_has_no_colours():  # scheduling then refuses
    assert u1.parse_file_colours({}, {}) == ()


def test_file_without_colours_lists_each_referenced_tool_as_unknown():
    assert u1.parse_file_colours({"referenced_tools": [0, 1, 2]}, {}) == (
        FileColour(0, None, None), FileColour(1, None, None), FileColour(2, None, None))


def test_file_without_colours_lists_each_used_filament_as_unknown():
    comments = u1.parse_gcode_comments("; filament used [mm] = 120.5, 0.00, 30.2\n")
    assert u1.parse_file_colours({}, comments) == (
        FileColour(0, None, None), FileColour(2, None, None))


def test_colours_from_metadata_used_lengths_skip_unused_filaments():  # the U1's own metadata
    meta = {"filament_colour": "#6F4C2F;#E72F1D;#FFFFFF;#F4C032", "filament_type": "PLA;PLA;PLA;PETG",
            "filament_used_mm": [0.0, 0.0, 2035.11, 2929.7]}
    assert u1.parse_file_colours(meta, {}) == (
        FileColour(2, "FFFFFF", "PLA"), FileColour(3, "F4C032", "PETG"))


def test_used_tool_past_the_colour_list_is_kept_as_unknown():  # so its head is still checked
    meta = {"filament_colors": ["#FF0000"], "filament_type": "PLA", "referenced_tools": [0, 2]}
    assert u1.parse_file_colours(meta, {}) == (
        FileColour(0, "FF0000", "PLA"), FileColour(2, None, None))
    comments = u1.parse_gcode_comments("; filament used [mm] = 5.0, 0.0, 7.5\n"
                                       "; filament_colour = #FFFFFF;#000000\n")
    assert u1.parse_file_colours({}, comments) == (
        FileColour(0, "FFFFFF", None), FileColour(2, None, None))


@pytest.mark.parametrize("lengths", [4964.8, [100.0], [100.0, float("nan"), 0.0], [5.0, "?", 0.0],
                                     [5.0, -1.0, 0.0]])
def test_filament_counts_as_unused_only_with_a_zero_length(lengths):  # anything else is checked
    meta = {"filament_colour": "#FFFFFF;#000000;#FF0000", "filament_type": "PLA;PLA;PLA",
            "filament_used_mm": lengths}
    logical = [c.logical for c in u1.parse_file_colours(meta, {})]
    assert logical[:2] == [0, 1]


@pytest.mark.parametrize("tools", [["x"], [-1], [0, "x"], [0, -2]])
def test_invalid_referenced_tools_mean_unknown(tools):  # every listed colour is then checked
    meta = {"filament_colour": "#FFFFFF;#000000", "filament_type": "PLA;PLA", "referenced_tools": tools}
    assert [c.logical for c in u1.parse_file_colours(meta, {})] == [0, 1]
    assert u1.needs_gcode_tail(meta)


def test_all_zero_lengths_check_every_listed_colour():
    meta = {"filament_colour": "#FFFFFF;#000000", "filament_type": "PLA;PLA",
            "filament_used_mm": [0.0, 0.0]}
    assert [c.logical for c in u1.parse_file_colours(meta, {})] == [0, 1]


def test_needs_tail_only_when_metadata_is_incomplete():
    assert u1.needs_gcode_tail({})
    assert u1.needs_gcode_tail({"filament_colors": ["#FFFFFF"]})
    assert u1.needs_gcode_tail({"filament_colors": ["#FFFFFF"], "referenced_tools": [0]})  # no types
    assert not u1.needs_gcode_tail({"filament_colors": ["#FFFFFF"], "filament_type": "PLA",
                                    "referenced_tools": [0]})
    assert not u1.needs_gcode_tail({"filament_colour": "#FFFFFF", "filament_type": "PLA",
                                    "filament_used_mm": [1.0]})


def test_file_info_picks_largest_thumbnail_in_subfolder():
    meta = {
        "size": 2048, "modified": 1760000000.5, "estimated_time": 3725,
        "thumbnails": [
            {"width": 32, "height": 32, "relative_path": ".thumbs/a-32x32.png"},
            {"width": 300, "height": 300, "relative_path": ".thumbs/a-300x300.png"},
        ],
        "filament_colors": ["#FFFFFF"], "filament_type": "PLA", "referenced_tools": [0],
    }
    info = u1.parse_file_info("plates/a b.gcode", meta, {})
    assert info.thumbnail == "plates/.thumbs/a-300x300.png"
    assert (info.size, info.modified, info.estimated_seconds) == (2048, 1760000000.5, 3725.0)
    assert info.colours == (FileColour(0, "FFFFFF", "PLA"),)


def test_head_map_script_and_check():
    assert u1.head_map_script([(0, 2), (1, 0)]) == (
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=2\n"
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=1 MAP_EXTRUDER=0"
    )
    table = {"extruder_map_table": [2, 0] + [0] * 30}
    assert u1.head_map_matches(table, [(0, 2), (1, 0)])
    assert not u1.head_map_matches(table, [(0, 1)])
    assert not u1.head_map_matches({}, [(0, 2)])


def test_history_entry_without_start_time_is_an_error():
    with pytest.raises(ValueError, match="without a start time"):
        u1.parse_history({"jobs": [{"filename": "a.gcode"}]})


@pytest.mark.parametrize("result", [{}, {"jobs": None}, {"count": 3}, {"jobs": {"a": 1}}, []])
def test_history_without_a_jobs_list_is_an_error(result):  # not "no prints"
    with pytest.raises(ValueError, match="jobs list"):
        u1.parse_history(result)


def test_history_entry_parses_to_utc_start_and_status():
    jobs = u1.parse_history({"count": 1, "jobs": [
        {"filename": "a.gcode", "start_time": 1760000000.25, "status": "completed"}]})
    assert jobs == [HistoryJob(
        "a.gcode", datetime(2025, 10, 9, 8, 53, 20, 250000, tzinfo=timezone.utc), "completed")]
    assert jobs[0].start_time.utcoffset() == timedelta(0)
    assert u1.parse_history({"count": 0, "jobs": []}) == []
