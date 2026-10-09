"""Per-channel occupancy (pure)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from opencoord.core.occupancy import ChannelOccupancy, channel_occupancy
from opencoord.core.types import Trace

MHZ = 1_000_000


@dataclass(frozen=True)
class Ch:
    number: int
    start_hz: int
    stop_hz: int


def _trace(levels: list[float], start_mhz: float = 100.0, step_khz: float = 1000.0) -> Trace:
    freqs = start_mhz * 1e6 + step_khz * 1e3 * np.arange(len(levels), dtype=np.float64)
    return Trace(freqs, np.asarray(levels, dtype=np.float32), "t")


def test_max_avg_and_percent_above() -> None:
    # Channel 1 covers bins at 100..103 MHz (4 bins), channel 2 bins 104..107.
    t = _trace([-100, -100, -60, -100, -50, -50, -50, -50])
    chs = [Ch(1, 100 * MHZ, 104 * MHZ), Ch(2, 104 * MHZ, 108 * MHZ)]
    occ = channel_occupancy(t, chs, floor_dbm=-100.0, threshold_db=10.0)
    assert [o.number for o in occ] == [1, 2]
    assert occ[0].max_dbm == pytest.approx(-60.0)
    assert occ[0].percent_above == pytest.approx(25.0)
    assert occ[1].max_dbm == pytest.approx(-50.0)
    assert occ[1].percent_above == pytest.approx(100.0)


def test_average_is_a_power_mean() -> None:
    t = _trace([-30, -40])
    (o,) = channel_occupancy(t, [Ch(1, 100 * MHZ, 102 * MHZ)], -100.0, 10.0)
    expected = 10 * np.log10((1e-3 + 1e-4) / 2)
    assert o.avg_dbm == pytest.approx(expected, abs=1e-4)


def test_level_exactly_at_the_limit_counts_as_above() -> None:
    t = _trace([-90, -91])
    (o,) = channel_occupancy(t, [Ch(1, 100 * MHZ, 102 * MHZ)], -100.0, 10.0)
    assert o.percent_above == pytest.approx(50.0)


def test_channels_without_bins_are_left_out() -> None:
    t = _trace([-80, -80, -80], start_mhz=100.0)
    chs = [Ch(1, 50 * MHZ, 60 * MHZ), Ch(2, 100 * MHZ, 103 * MHZ), Ch(3, 200 * MHZ, 210 * MHZ)]
    assert [o.number for o in channel_occupancy(t, chs, -100.0, 10.0)] == [2]


def test_upper_edge_is_exclusive() -> None:
    t = _trace([-80, -80, -20])  # the -20 dBm bin sits on 102 MHz, the edge of channel 1
    chs = [Ch(1, 100 * MHZ, 102 * MHZ), Ch(2, 102 * MHZ, 104 * MHZ)]
    occ = channel_occupancy(t, chs, -100.0, 10.0)
    assert occ[0].max_dbm == pytest.approx(-80.0)
    assert occ[1].max_dbm == pytest.approx(-20.0)


def test_empty_trace_and_returns_dataclass() -> None:
    empty = Trace(np.array([], dtype=np.float64), np.array([], dtype=np.float32), "e")
    assert channel_occupancy(empty, [Ch(1, 0, MHZ)], -100.0, 10.0) == []
    t = _trace([-80])
    (o,) = channel_occupancy(t, [Ch(1, 100 * MHZ, 101 * MHZ)], -100.0, 10.0)
    assert isinstance(o, ChannelOccupancy)


def test_full_channel_has_full_coverage() -> None:
    t = _trace([-80] * 10)  # 100..109 MHz
    (o,) = channel_occupancy(t, [Ch(1, 101 * MHZ, 105 * MHZ)], -100.0, 10.0)
    assert o.coverage == pytest.approx(1.0)


def test_half_covered_channel_reports_half_coverage() -> None:
    t = _trace([-80] * 5)  # 100..104 MHz
    (o,) = channel_occupancy(t, [Ch(1, 102 * MHZ, 106 * MHZ)], -100.0, 10.0)
    assert o.coverage == pytest.approx(0.5)


def test_trace_ending_on_a_channel_edge_gives_the_next_channel_zero_coverage() -> None:
    t = _trace([-80, -80, -20])  # last bin exactly on 102 MHz
    occ = channel_occupancy(
        t, [Ch(1, 100 * MHZ, 102 * MHZ), Ch(2, 102 * MHZ, 104 * MHZ)], -100.0, 10.0
    )
    assert [o.number for o in occ] == [1, 2]
    assert occ[0].coverage == pytest.approx(1.0)
    assert occ[1].coverage == pytest.approx(0.0)
