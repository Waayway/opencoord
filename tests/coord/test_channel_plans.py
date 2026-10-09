"""Channel plans: the pure parser and the packaged eu.toml."""

from __future__ import annotations

import pytest

from opencoord.coord import channel_plans
from opencoord.coord.channel_plans import ChannelPlan, parse_plan

MHZ = 1_000_000

SMALL = """
[plan]
title = "Test"
[[channels]]
first = 1
last = 3
centre_base_mhz = 100
width_mhz = 10
pmse = "allowed"
[[bands]]
start_mhz = 200
stop_mhz = 210
pmse = "forbidden"
note = "no"
"""


def test_parse_builds_channels_from_the_raster() -> None:
    plan = parse_plan(SMALL, name="t")
    assert plan.name == "t"
    assert plan.title == "Test"
    assert [c.number for c in plan.channels] == [1, 2, 3]
    ch2 = plan.channels[1]
    assert (ch2.start_hz, ch2.stop_hz, ch2.centre_hz) == (115 * MHZ, 125 * MHZ, 120 * MHZ)
    assert ch2.pmse == "allowed"
    assert plan.bands[0].start_hz == 200 * MHZ and plan.bands[0].pmse == "forbidden"
    assert plan.bands[0].note == "no"


def test_channel_at_uses_half_open_edges() -> None:
    plan = parse_plan(SMALL, name="t")
    assert plan.channel_at(105 * MHZ) is plan.channels[0]
    assert plan.channel_at(115 * MHZ) is plan.channels[1]  # shared edge belongs to the upper one
    assert plan.channel_at(95 * MHZ - 1) is None
    assert plan.channel_at(135 * MHZ) is None


def test_shaded_spans_merge_contiguous_channels_and_add_bands() -> None:
    spans = parse_plan(SMALL, name="t").shaded_spans()
    assert [(s.start_hz, s.stop_hz, s.pmse) for s in spans] == [
        (105 * MHZ, 135 * MHZ, "allowed"),
        (200 * MHZ, 210 * MHZ, "forbidden"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "[plan]\n[[channels]]\nfirst=3\nlast=1\ncentre_base_mhz=1\nwidth_mhz=1\n",
        "[plan]\n[[channels]]\nfirst=1\nlast=1\ncentre_base_mhz=1\nwidth_mhz=0\n",
        "[plan]\n[[bands]]\nstart_mhz=5\nstop_mhz=5\npmse='allowed'\n",
        "[plan]\n[[bands]]\nstart_mhz=1\nstop_mhz=5\npmse='maybe'\n",
        "[plan]\n[[bands]]\nstart_mhz=1\nstop_mhz=5\n",
        "not toml [",
    ],
)
def test_invalid_plans_raise_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_plan(text, name="bad")


def test_available_lists_eu() -> None:
    assert "eu" in channel_plans.available()


def test_eu_dvb_t_raster() -> None:
    plan = channel_plans.load("eu")
    assert isinstance(plan, ChannelPlan)
    numbers = [c.number for c in plan.channels]
    assert numbers == list(range(21, 49))
    ch21, ch48 = plan.channels[0], plan.channels[-1]
    assert (ch21.start_hz, ch21.centre_hz, ch21.stop_hz) == (470 * MHZ, 474 * MHZ, 478 * MHZ)
    assert ch48.stop_hz == 694 * MHZ
    assert all(c.pmse == "allowed" for c in plan.channels)
    assert all(c.stop_hz - c.start_hz == 8 * MHZ for c in plan.channels)


def _band_at(plan: ChannelPlan, mhz: float) -> str | None:
    for b in plan.bands:
        if b.start_hz <= mhz * MHZ < b.stop_hz:
            return b.pmse
    return None


def test_eu_band_annotations() -> None:
    plan = channel_plans.load("eu")
    assert _band_at(plan, 700) == "forbidden"
    assert _band_at(plan, 789.9) == "forbidden"
    assert _band_at(plan, 800) == "forbidden"
    assert _band_at(plan, 827) == "allowed"
    assert _band_at(plan, 850) == "forbidden"
    assert _band_at(plan, 864) == "allowed"
    assert _band_at(plan, 1790) == "info"
    notes = {b.note for b in plan.bands}
    assert "700 MHz mobile band (not for PMSE in NL)" in notes
    assert "Mic duplex gap (823-832 MHz)" in notes
    assert "Licence-free (863-865 MHz)" in notes
    assert "PMSE 1785-1805 MHz (licence)" in notes


def test_load_unknown_plan_raises() -> None:
    with pytest.raises(FileNotFoundError):
        channel_plans.load("atlantis")
