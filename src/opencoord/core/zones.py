"""User exclusion zones: pure helpers over a list of ``ExclusionZone`` (sessions and the solver
use the same list). All functions return new lists and raise ``ValueError`` with a user-facing
message when the change is not allowed."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from opencoord.core.types import ExclusionZone

#: Exclusion zones that can exist at once.
MAX_EXCLUSION_ZONES: Final = 16


def normalize(start_hz: float, stop_hz: float) -> tuple[int, int]:
    """``(start, stop)`` in Hz, ordered; ``ValueError`` for an empty or negative range."""
    lo, hi = sorted((round(start_hz), round(stop_hz)))
    if lo < 0 or hi <= lo:
        raise ValueError("An exclusion zone needs a start below its stop")
    return lo, hi


def add_zone(
    zones: Sequence[ExclusionZone], start_hz: float, stop_hz: float
) -> tuple[list[ExclusionZone], int]:
    """``(new list ordered by id, id of the new zone)``; the smallest free id is used."""
    if len(zones) >= MAX_EXCLUSION_ZONES:
        raise ValueError(f"At most {MAX_EXCLUSION_ZONES} exclusion zones; remove one first")
    lo, hi = normalize(start_hz, stop_hz)
    used = {z.id for z in zones}
    zone_id = next(i for i in range(1, MAX_EXCLUSION_ZONES + 1) if i not in used)
    return sorted([*zones, ExclusionZone(zone_id, lo, hi)], key=lambda z: z.id), zone_id


def update_zone(
    zones: Sequence[ExclusionZone], zone_id: int, start_hz: float, stop_hz: float
) -> list[ExclusionZone]:
    """``zones`` with zone ``zone_id`` moved; an unknown id leaves them unchanged."""
    lo, hi = normalize(start_hz, stop_hz)
    return [ExclusionZone(zone_id, lo, hi) if z.id == zone_id else z for z in zones]


def remove_zone(zones: Sequence[ExclusionZone], zone_id: int) -> list[ExclusionZone]:
    return [z for z in zones if z.id != zone_id]


def contains(zones: Sequence[ExclusionZone], freq_hz: float) -> bool:
    """Whether ``freq_hz`` lies inside any zone (edges inclusive)."""
    return any(z.start_hz <= freq_hz <= z.stop_hz for z in zones)
