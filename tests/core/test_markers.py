"""Marker math: level lookup, peak search, delta."""

import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.core.markers import (
    MAX_MARKERS,
    Marker,
    delta,
    level_at,
    next_peak,
    peak,
)
from opencoord.core.types import Trace


def _trace(dbm: object, start: float = 470e6, step: float = 100e3, label: str = "t") -> Trace:
    arr = np.asarray(dbm, dtype=np.float32)
    freqs: npt.NDArray[np.float64] = start + step * np.arange(len(arr), dtype=np.float64)
    return Trace(freqs, arr, label)


def test_marker_is_frozen_and_limit() -> None:
    m = Marker(1, 470_000_000, "max")
    with pytest.raises(AttributeError):
        m.freq_hz = 1  # type: ignore[misc]
    assert MAX_MARKERS == 8


def test_level_at_nearest_bin() -> None:
    t = _trace([-90, -80, -70])
    assert level_at(t, 470_100_000) == -80.0
    assert level_at(t, 470_140_000) == -80.0
    assert level_at(t, 470_160_000) == -70.0


def test_level_at_outside_range_is_none() -> None:
    t = _trace([-90, -80, -70])
    assert level_at(t, 469_000_000) is None
    assert level_at(t, 471_000_000) is None
    assert level_at(t, t.stop_hz) == -70.0


def test_level_at_single_point_trace() -> None:
    t = _trace([-60])
    assert level_at(t, 470_000_000) == -60.0


def test_peak_is_global_maximum() -> None:
    t = _trace([-90, -50, -80, -60])
    assert peak(t) == 470_100_000


def test_peak_empty_raises() -> None:
    with pytest.raises(ValueError):
        peak(_trace([]))


def test_next_peak_right_and_left() -> None:
    t = _trace([-100, -60, -100, -100, -70, -100, -100, -50, -100])
    assert next_peak(t, 470_100_000, "right") == 470_400_000
    assert next_peak(t, 470_400_000, "right") == 470_700_000
    assert next_peak(t, 470_700_000, "left") == 470_400_000
    assert next_peak(t, 470_400_000, "left") == 470_100_000


def test_next_peak_none_at_the_end() -> None:
    t = _trace([-100, -60, -100, -50, -100])
    assert next_peak(t, 470_300_000, "right") is None
    assert next_peak(t, 470_100_000, "left") is None


def test_next_peak_ignores_ripple_below_prominence() -> None:
    t = _trace([-100, -60, -100, -61, -100, -70, -100])
    assert next_peak(t, 470_100_000, "right", min_prominence_db=3.0) == 470_300_000
    assert next_peak(t, 470_100_000, "right", min_prominence_db=50.0) is None


def test_next_peak_from_between_bins() -> None:
    t = _trace([-100, -60, -100, -100, -70, -100])
    assert next_peak(t, 470_250_000, "right") == 470_400_000
    assert next_peak(t, 470_250_000, "left") == 470_100_000


@given(
    st.lists(st.floats(-120, -20, width=32), min_size=1, max_size=60),
    st.integers(469_000_000, 477_000_000),
)
def test_next_peak_direction_invariant(levels: list[float], from_hz: int) -> None:
    t = _trace(levels)
    r = next_peak(t, from_hz, "right")
    left = next_peak(t, from_hz, "left")
    assert r is None or r > from_hz
    assert left is None or left < from_hz


def test_delta_between_markers() -> None:
    a_t = _trace([-90, -80, -70], label="a")
    b_t = _trace([-60, -65, -75], label="b")
    a = Marker(1, 470_200_000, "max")
    b = Marker(2, 470_000_000, "live")
    df, ddb = delta(a, b, {"max": a_t, "live": b_t})
    assert df == 200_000
    assert ddb == pytest.approx(-70.0 - -60.0)


def test_delta_missing_level_raises() -> None:
    t = _trace([-90, -80])
    a = Marker(1, 480_000_000, "max")
    b = Marker(2, 470_000_000, "max")
    with pytest.raises(ValueError):
        delta(a, b, {"max": t})
