"""Pure display helpers: resampling to display bins and the waterfall history."""

from __future__ import annotations

import numpy as np
import pytest

from opencoord.core.types import Trace
from opencoord.ui.state import WaterfallHistory, resample_max

MHZ = 1_000_000


def _trace(start: float, stop: float, dbm: list[float]) -> Trace:
    freqs = np.linspace(start, stop, len(dbm), dtype=np.float64)
    return Trace(freqs, np.array(dbm, dtype=np.float32), "t")


def test_downsampling_keeps_the_max_of_each_bin() -> None:
    freqs = np.arange(8, dtype=np.float64)  # 0..7 Hz
    dbm = np.array([-100, -90, -100, -100, -100, -100, -100, -50], dtype=np.float32)
    out = resample_max(freqs, dbm, 0.0, 8.0, 4)
    assert out.dtype == np.float32
    assert out.tolist() == [-90, -100, -100, -50]


def test_narrow_carrier_survives_heavy_downsampling() -> None:
    freqs = np.linspace(470 * MHZ, 960 * MHZ, 6000)
    dbm = np.full(6000, -105.0, dtype=np.float32)
    dbm[3001] = -40.0
    out = resample_max(freqs, dbm, 470 * MHZ, 960 * MHZ, 1024)
    assert out.max() == pytest.approx(-40.0)
    assert np.count_nonzero(out > -100) == 1


def test_upsampling_interpolates_and_blanks_outside_the_data() -> None:
    freqs = np.array([10.0, 20.0])
    dbm = np.array([-100.0, -80.0], dtype=np.float32)
    out = resample_max(freqs, dbm, 0.0, 30.0, 30)
    assert np.isnan(out[:10]).all() and np.isnan(out[21:]).all()
    assert out[15] == pytest.approx(-89.5, abs=0.6)
    assert not np.isnan(out[10:21]).any()


def test_history_newest_row_first_and_bounded() -> None:
    h = WaterfallHistory(depth=3, bins=4)
    assert h.count == 0 and h.range_hz is None and np.isnan(h.rows).all()
    versions = [h.version]
    for level in (-100.0, -90.0, -80.0, -70.0):
        h.push(_trace(0, 3, [level] * 4))
        versions.append(h.version)
    assert versions == sorted(set(versions))  # every push is a new version
    assert h.count == 3
    assert h.range_hz == (0, 3)
    assert h.rows[:, 0].tolist() == [-70.0, -80.0, -90.0]


def test_history_clears_when_the_range_changes() -> None:
    h = WaterfallHistory(depth=3, bins=4)
    h.push(_trace(0, 3, [-90.0] * 4))
    h.push(_trace(0, 3, [-80.0] * 4))
    h.push(_trace(10, 13, [-70.0] * 4))
    assert h.count == 1 and h.range_hz == (10, 13)
    assert np.isnan(h.rows[1:]).all()


def test_history_depth_change_keeps_newest_rows() -> None:
    h = WaterfallHistory(depth=3, bins=2)
    for level in (-100.0, -90.0, -80.0):
        h.push(_trace(0, 1, [level] * 2))
    h.set_depth(2)
    assert h.rows.shape == (2, 2) and h.count == 2
    assert h.rows[:, 0].tolist() == [-80.0, -90.0]
    h.set_depth(4)
    assert h.rows.shape == (4, 2) and h.count == 2
    assert np.isnan(h.rows[2:]).all()
    with pytest.raises(ValueError):
        h.set_depth(0)


def test_history_clear() -> None:
    h = WaterfallHistory(depth=2, bins=2)
    h.push(_trace(0, 1, [-1.0, -2.0]))
    v = h.version
    h.clear()
    assert h.count == 0 and h.range_hz is None and h.version == v + 1


def test_history_counters_for_incremental_views() -> None:
    h = WaterfallHistory(depth=3, bins=2)
    g = h.generation
    h.push(_trace(0, 1, [-1.0, -2.0]))  # first push sets the range: a clear
    assert h.generation == g + 1 and h.pushes == 1
    h.push(_trace(0, 1, [-1.0, -2.0]))
    assert h.generation == g + 1 and h.pushes == 2
    h.set_depth(5)
    assert h.generation == g + 2
