"""Which parts of a time window were not watched (pure)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


def uncovered_gaps(periods: Iterable[Period], start: datetime, end: datetime,
                   tolerance: timedelta) -> list[Period]:
    """Parts of [start, end] outside every period that are longer than `tolerance`."""
    clipped = sorted(
        (max(p.start, start), min(p.end, end)) for p in periods if p.end > start and p.start < end
    )
    gaps: list[Period] = []
    cursor = start
    for begin, finish in clipped:
        if begin > cursor:
            gaps.append(Period(cursor, begin))
        cursor = max(cursor, finish)
    if cursor < end:
        gaps.append(Period(cursor, end))
    return [gap for gap in gaps if gap.end - gap.start > tolerance]
