"""Choosing heads for a file's colours, and describing filament in text."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from app.printer.models import HEAD_COUNT, FileColour, HeadFilament

from .models import HeadAssignment

NO_COLOUR_DISTANCE = 1000.0  # larger than any real RGB distance (max ≈ 442)


class HeadChoiceError(ValueError):
    pass


def describe_filament(rgb: str | None, material: str | None) -> str:
    material = material or "unknown material"
    return f"#{rgb} {material}" if rgb else f"unknown colour, {material}"


def rgb_distance(a: str | None, b: str | None) -> float:
    if not a or not b:
        return NO_COLOUR_DISTANCE
    return math.dist([int(a[i:i + 2], 16) for i in (0, 2, 4)],
                     [int(b[i:i + 2], 16) for i in (0, 2, 4)])


def _same_material(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


def suggest_heads(colours: Sequence[FileColour], heads: Sequence[HeadFilament]) -> dict[int, int]:
    """Per colour: a loaded head with the same material, then the nearest colour."""
    loaded = [h for h in heads if h.present]
    suggestion: dict[int, int] = {}
    for colour in colours:
        if not loaded:
            suggestion[colour.logical] = colour.logical % HEAD_COUNT
            continue
        best = min(loaded, key=lambda h: (not _same_material(colour.material, h.material),
                                          rgb_distance(colour.rgb, h.rgb), h.head))
        suggestion[colour.logical] = best.head
    return suggestion


def build_assignments(choice: Mapping[int, int], colours: Sequence[FileColour],
                      heads: Sequence[HeadFilament]) -> tuple[HeadAssignment, ...]:
    """Turn the user's head choices into assignments that remember what was loaded."""
    if not colours:
        raise HeadChoiceError("this file has no colour information, so its heads can't be checked")
    by_head = {h.head: h for h in heads}
    assignments = []
    for colour in colours:
        if colour.logical not in choice:
            raise HeadChoiceError(f"pick a head for colour {colour.logical + 1}")
        head = by_head.get(choice[colour.logical])
        if head is None:
            raise HeadChoiceError(f"head {choice[colour.logical] + 1} does not exist")
        if not head.present:
            raise HeadChoiceError(f"head {head.head + 1} is empty")
        if head.material is None or head.rgb is None:
            raise HeadChoiceError(
                f"head {head.head + 1}: filament not identified; set its type and colour on the printer")
        assignments.append(HeadAssignment(colour.logical, head.head, head.material, head.rgb))
    return tuple(assignments)
