"""Exclusion zone helpers (pure)."""

from __future__ import annotations

import pytest

from opencoord.core.types import ExclusionZone
from opencoord.core.zones import (
    MAX_EXCLUSION_ZONES,
    add_zone,
    contains,
    normalize,
    remove_zone,
    update_zone,
)

MHZ = 1_000_000


def test_normalize_orders_and_rejects_empty() -> None:
    assert normalize(520 * MHZ, 510 * MHZ) == (510 * MHZ, 520 * MHZ)
    for bad in ((5, 5), (-5, 10)):
        with pytest.raises(ValueError, match="start below its stop"):
            normalize(*bad)


def test_add_uses_the_smallest_free_id_and_keeps_order() -> None:
    zones, a = add_zone([], 1 * MHZ, 2 * MHZ)
    zones, b = add_zone(zones, 3 * MHZ, 4 * MHZ)
    zones = remove_zone(zones, a)
    zones, c = add_zone(zones, 5 * MHZ, 6 * MHZ)
    assert (a, b, c) == (1, 2, 1)
    assert [z.id for z in zones] == [1, 2]


def test_add_respects_the_limit() -> None:
    zones: list[ExclusionZone] = []
    for i in range(MAX_EXCLUSION_ZONES):
        zones, _ = add_zone(zones, i * MHZ, i * MHZ + 1000)
    with pytest.raises(ValueError, match=str(MAX_EXCLUSION_ZONES)):
        add_zone(zones, 100 * MHZ, 101 * MHZ)


def test_update_and_contains() -> None:
    zones, zid = add_zone([], 10 * MHZ, 20 * MHZ)
    zones = update_zone(zones, zid, 30 * MHZ, 25 * MHZ)
    assert (zones[0].start_hz, zones[0].stop_hz) == (25 * MHZ, 30 * MHZ)
    assert contains(zones, 25 * MHZ) and contains(zones, 30 * MHZ)
    assert not contains(zones, 24 * MHZ)
    assert update_zone(zones, 99, 1, 2) == zones
    with pytest.raises(ValueError):
        update_zone(zones, zid, 5, 5)
