"""Built-in frequency range presets (plan §4)."""

from __future__ import annotations

from opencoord.core.presets import OVERVIEW, PRESETS, RangePreset, available, find

MHZ = 1_000_000


def test_builtin_table_matches_the_plan() -> None:
    table = {p.name: (p.start_hz, p.stop_hz) for p in PRESETS}
    assert table == {
        "Full UHF 470-960": (470 * MHZ, 960 * MHZ),
        "TV 21-48 (470-694)": (470 * MHZ, 694 * MHZ),
        "694-790": (694 * MHZ, 790 * MHZ),
        "823-832": (823 * MHZ, 832 * MHZ),
        "863-865": (863 * MHZ, 865 * MHZ),
        "1785-1805": (1785 * MHZ, 1805 * MHZ),
        "ISM 433": (433_050_000, 434_790_000),
        "ISM 868": (863 * MHZ, 870 * MHZ),
        "VHF 174-216": (174 * MHZ, 216 * MHZ),
    }
    assert all(p.start_hz < p.stop_hz for p in PRESETS)


def test_without_a_device_every_fixed_preset_is_offered() -> None:
    assert available(None) == list(PRESETS)


def test_presets_outside_the_device_range_are_hidden() -> None:
    names = [p.name for p in available((50_000, 960 * MHZ))]
    assert "1785-1805" not in names
    assert "Full UHF 470-960" in names
    assert names[-1] == OVERVIEW


def test_overview_spans_the_device_range() -> None:
    overview = available((240 * MHZ, 480 * MHZ))[-1]
    assert overview == RangePreset(OVERVIEW, 240 * MHZ, 480 * MHZ)
    names = [p.name for p in available((240 * MHZ, 480 * MHZ))]
    assert names == ["ISM 433", OVERVIEW]  # partially covered ranges are hidden too


def test_find_by_name() -> None:
    assert find("694-790", None) == RangePreset("694-790", 694 * MHZ, 790 * MHZ)
    assert find(OVERVIEW, (1 * MHZ, 2 * MHZ)) == RangePreset(OVERVIEW, 1 * MHZ, 2 * MHZ)
    assert find(OVERVIEW, None) is None
    assert find("nope", None) is None
