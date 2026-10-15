import pytest

from app.jobs.heads import (
    HeadChoiceError, build_assignments, describe_filament, suggest_heads,
)
from app.jobs.models import HeadAssignment
from app.printer.models import FileColour, HeadFilament
from tests.factories import DEFAULT_HEADS  # white PLA, black PLA, red PETG, empty


def test_prefers_same_material_then_nearest_colour():
    assert suggest_heads((FileColour(0, "FF1010", "PLA"),), DEFAULT_HEADS) == {0: 1}


def test_unknown_material_uses_nearest_colour():
    assert suggest_heads((FileColour(0, "FF1010", None),), DEFAULT_HEADS) == {0: 2}


def test_no_loaded_heads_falls_back_to_same_number():
    empty = tuple(HeadFilament(i, False) for i in range(4))
    colours = (FileColour(0, None, None), FileColour(1, None, None))
    assert suggest_heads(colours, empty) == {0: 0, 1: 1}


def test_build_assignments_copies_loaded_filament():
    colours = (FileColour(0, "FF0000", "PETG"),)
    assert build_assignments({0: 2}, colours, DEFAULT_HEADS) == (HeadAssignment(0, 2, "PETG", "FF0000"),)


def test_build_assignments_rejects_bad_choices():
    colours = (FileColour(0, None, None),)
    with pytest.raises(HeadChoiceError, match="head 4 is empty"):
        build_assignments({0: 3}, colours, DEFAULT_HEADS)
    with pytest.raises(HeadChoiceError, match="pick a head for colour 1"):
        build_assignments({}, colours, DEFAULT_HEADS)
    with pytest.raises(HeadChoiceError, match="head 9 does not exist"):
        build_assignments({0: 8}, colours, DEFAULT_HEADS)


def test_describe_filament():
    assert describe_filament("FF0000", "PLA") == "#FF0000 PLA"
    assert describe_filament(None, None) == "unknown colour, unknown material"


def test_build_assignments_rejects_file_without_colours():
    with pytest.raises(HeadChoiceError, match="this file has no colour information, so its heads can't be checked"):
        build_assignments({}, (), DEFAULT_HEADS)


@pytest.mark.parametrize("head", [
    HeadFilament(2, True, "PETG", "Generic", None),
    HeadFilament(2, True, None, "Generic", "FF0000"),
])
def test_build_assignments_rejects_unidentified_filament(head):
    colours = (FileColour(0, "FF0000", "PETG"),)
    heads = (DEFAULT_HEADS[0], DEFAULT_HEADS[1], head, DEFAULT_HEADS[3])
    with pytest.raises(HeadChoiceError, match="head 3: filament not identified; set its type and colour on the printer"):
        build_assignments({0: 2}, colours, heads)
