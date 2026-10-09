import numpy as np
import numpy.typing as npt
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from opencoord.core.traces import (
    TraceSet,
    _prominence,
    detected_carriers,
    find_peaks,
    noise_floor,
)
from opencoord.core.types import Carrier, Sweep, Trace


def _freqs(n: int, start: float = 470e6, step: float = 100e3) -> npt.NDArray[np.float64]:
    return start + step * np.arange(n, dtype=np.float64)


def _sweep(dbm: object, start: float = 470e6, ts: float = 0.0) -> Sweep:
    arr = np.asarray(dbm, dtype=np.float32)
    return Sweep(_freqs(len(arr), start), arr, ts)


# --- Trace type ---------------------------------------------------------------------------------


def test_trace_start_stop_and_carrier() -> None:
    t = Trace(_freqs(5), np.zeros(5, dtype=np.float32), "x")
    assert (t.start_hz, t.stop_hz, t.label) == (470_000_000, 470_400_000, "x")
    assert Carrier(1, -50.0) == Carrier(1, -50.0)


# --- TraceSet -----------------------------------------------------------------------------------


def test_empty_traceset_has_no_traces() -> None:
    ts = TraceSet()
    assert ts.live is ts.max_hold is ts.average is ts.min_hold is None


def test_single_update_all_traces_equal_sweep() -> None:
    ts = TraceSet()
    ts.update(_sweep([-90, -80, -70]))
    for tr in (ts.live, ts.max_hold, ts.average, ts.min_hold):
        assert tr is not None
        np.testing.assert_array_equal(tr.dbm, np.array([-90, -80, -70], dtype=np.float32))
        assert tr.dbm.dtype == np.float32 and tr.freqs_hz.dtype == np.float64
    assert [t.label for t in (ts.live, ts.max_hold, ts.average, ts.min_hold) if t] == [
        "Live",
        "Max hold",
        "Average",
        "Min hold",
    ]


def test_max_and_min_hold_accumulate() -> None:
    ts = TraceSet()
    ts.update(_sweep([-90, -60, -70]))
    ts.update(_sweep([-80, -90, -70]))
    assert ts.max_hold is not None and ts.min_hold is not None and ts.live is not None
    np.testing.assert_array_equal(ts.max_hold.dbm, np.array([-80, -60, -70], dtype=np.float32))
    np.testing.assert_array_equal(ts.min_hold.dbm, np.array([-90, -90, -70], dtype=np.float32))
    np.testing.assert_array_equal(ts.live.dbm, np.array([-80, -90, -70], dtype=np.float32))


def test_average_is_exact_mean_of_last_n() -> None:
    ts = TraceSet(average_count=3)
    for v in (-100.0, -90.0, -80.0, -70.0):
        ts.update(_sweep([v]))
    assert ts.average is not None
    assert ts.average.dbm[0] == pytest.approx(-80.0)  # mean(-90, -80, -70)
    # max/min hold are not windowed
    assert ts.min_hold is not None and ts.min_hold.dbm[0] == -100.0


def test_average_count_change_trims_window() -> None:
    ts = TraceSet(average_count=4)
    for v in (-100.0, -90.0, -80.0, -70.0):
        ts.update(_sweep([v]))
    ts.average_count = 2
    assert ts.average_count == 2
    assert ts.average is not None and ts.average.dbm[0] == pytest.approx(-75.0)
    ts.update(_sweep([-60.0]))
    assert ts.average is not None and ts.average.dbm[0] == pytest.approx(-65.0)


def test_average_count_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TraceSet(average_count=0)
    with pytest.raises(ValueError):
        TraceSet().average_count = 0


def test_reset_clears_everything_but_keeps_average_count() -> None:
    ts = TraceSet(average_count=5)
    ts.update(_sweep([-90, -80]))
    ts.reset()
    assert ts.live is ts.max_hold is ts.average is ts.min_hold is None
    assert ts.average_count == 5
    ts.update(_sweep([-50, -60]))
    assert ts.max_hold is not None
    np.testing.assert_array_equal(ts.max_hold.dbm, np.array([-50, -60], dtype=np.float32))


@pytest.mark.parametrize(
    "second",
    [
        _sweep([-50, -50, -50, -50]),  # different length
        _sweep([-50, -50, -50], start=500e6),  # different start/stop
    ],
)
def test_axis_change_resets_all(second: Sweep) -> None:
    ts = TraceSet()
    ts.update(_sweep([-10, -10, -10]))
    ts.update(second)
    assert ts.max_hold is not None and ts.average is not None and ts.min_hold is not None
    np.testing.assert_array_equal(ts.max_hold.dbm, second.dbm)
    np.testing.assert_array_equal(ts.min_hold.dbm, second.dbm)
    np.testing.assert_array_equal(ts.average.dbm, second.dbm)
    assert ts.max_hold.start_hz == second.start_hz


def test_traces_do_not_alias_input_or_each_other() -> None:
    ts = TraceSet()
    s = _sweep([-90, -80])
    ts.update(s)
    s.dbm[0] = 0.0
    assert ts.live is not None and ts.live.dbm[0] == -90.0
    first_max = ts.max_hold
    ts.update(_sweep([-10, -10]))
    assert first_max is not None and first_max.dbm[0] == -90.0  # old object not mutated


@given(st.integers(1, 12), st.integers(1, 5), st.data())
def test_invariants(sweeps: int, n_avg: int, data: st.DataObject) -> None:
    n = data.draw(st.integers(1, 6))
    ts = TraceSet(average_count=n_avg)
    for _ in range(sweeps):
        dbm = data.draw(
            hnp.arrays(np.float32, n, elements=st.floats(-130, 0, width=32, allow_nan=False))
        )
        ts.update(_sweep(dbm))
        assert ts.live and ts.max_hold and ts.min_hold and ts.average
        assert np.all(ts.max_hold.dbm >= ts.live.dbm)
        assert np.all(ts.live.dbm >= ts.min_hold.dbm)
        assert np.all(ts.average.dbm >= ts.min_hold.dbm)
        assert np.all(ts.average.dbm <= ts.max_hold.dbm)
    ts.reset()
    assert ts.live is None and ts.max_hold is None and ts.min_hold is None and ts.average is None


# --- noise_floor --------------------------------------------------------------------------------


def test_noise_floor_of_flat_trace() -> None:
    assert noise_floor(np.full(100, -95.0, dtype=np.float32)) == pytest.approx(-95.0)


def test_noise_floor_empty_raises() -> None:
    with pytest.raises(ValueError):
        noise_floor(np.array([], dtype=np.float32))


def test_noise_floor_stable_with_40_percent_dvbt_occupancy() -> None:
    rng = np.random.default_rng(1)
    noise = rng.normal(-100, 1.0, 1000)
    clean = noise_floor(noise.astype(np.float32))
    occupied = noise.copy()
    occupied[300:700] = rng.normal(-60, 2.0, 400)  # 40 % strong block
    dirty = noise_floor(occupied.astype(np.float32))
    assert abs(clean - (-100)) < 1.5
    assert abs(dirty - clean) < 1.5


@given(
    hnp.arrays(np.float32, st.integers(1, 50), elements=st.floats(-130, 0, width=32)),
)
def test_noise_floor_within_data_range(dbm: npt.NDArray[np.float32]) -> None:
    f = noise_floor(dbm)
    assert float(dbm.min()) <= f <= float(dbm.max())


# --- find_peaks ---------------------------------------------------------------------------------


def test_find_peaks_orders_strongest_first() -> None:
    dbm = np.full(50, -100.0, dtype=np.float32)
    dbm[10], dbm[30], dbm[40] = -60, -50, -70
    peaks = find_peaks(_freqs(50), dbm, min_prominence_db=10, min_spacing_hz=0)
    assert peaks == [30, 10, 40]


def test_find_peaks_prominence_filters_small_bumps() -> None:
    dbm = np.full(50, -100.0, dtype=np.float32)
    dbm[10] = -60
    dbm[11] = -90  # shoulder
    dbm[12] = -85  # bump on shoulder, prominence 5 dB
    peaks = find_peaks(_freqs(50), dbm, min_prominence_db=10, min_spacing_hz=0)
    assert peaks == [10]
    peaks = find_peaks(_freqs(50), dbm, min_prominence_db=3, min_spacing_hz=0)
    assert peaks == [10, 12]


def test_find_peaks_prominence_uses_higher_base() -> None:
    # Two peaks joined by a high saddle: the lower peak's prominence is relative to the saddle.
    dbm = np.full(21, -100.0, dtype=np.float32)
    dbm[5], dbm[10], dbm[15] = -40, -45, -50
    dbm[6:10] = -48
    # Peak at 10 (-45): the nearest higher sample on the left is at 5 and the minimum in between
    # is -48, so its left base is -48; its right base is -100. Prominence = -45 - (-48) = 3 dB.
    assert find_peaks(_freqs(21), dbm, min_prominence_db=4, min_spacing_hz=0) == [5, 15]
    assert 10 in find_peaks(_freqs(21), dbm, min_prominence_db=3, min_spacing_hz=0)


def test_find_peaks_plateau_reports_middle() -> None:
    dbm = np.full(20, -100.0, dtype=np.float32)
    dbm[5:10] = -50
    assert find_peaks(_freqs(20), dbm, min_prominence_db=10, min_spacing_hz=0) == [7]


def test_find_peaks_edges_can_be_peaks() -> None:
    dbm = np.array([-50, -100, -100, -80], dtype=np.float32)
    assert find_peaks(_freqs(4), dbm, min_prominence_db=10, min_spacing_hz=0) == [0, 3]


def test_find_peaks_spacing_suppresses_weaker_neighbour() -> None:
    dbm = np.full(50, -100.0, dtype=np.float32)
    dbm[10], dbm[12], dbm[30] = -50, -40, -60
    # step 100 kHz: indices 10 and 12 are 200 kHz apart
    assert find_peaks(_freqs(50), dbm, 10, min_spacing_hz=300_000) == [12, 30]
    assert find_peaks(_freqs(50), dbm, 10, min_spacing_hz=200_000) == [12, 10, 30]


def test_find_peaks_degenerate_inputs() -> None:
    assert find_peaks(np.array([]), np.array([], dtype=np.float32), 3, 0) == []
    assert find_peaks(_freqs(1), np.array([-50], dtype=np.float32), 3, 0) == [0]
    flat = np.full(10, -90.0, dtype=np.float32)
    assert find_peaks(_freqs(10), flat, 3, 0) == []  # flat: prominence 0


@given(
    hnp.arrays(np.float32, st.integers(1, 60), elements=st.floats(-130, 0, width=32)),
    st.floats(0, 20),
    st.integers(0, 1_000_000),
)
def test_find_peaks_invariants(dbm: npt.NDArray[np.float32], prom: float, spacing: int) -> None:
    freqs = _freqs(len(dbm))
    peaks = find_peaks(freqs, dbm, prom, spacing)
    assert len(set(peaks)) == len(peaks)
    levels = [float(dbm[i]) for i in peaks]
    assert levels == sorted(levels, reverse=True)
    for a in range(len(peaks)):
        for b in range(a + 1, len(peaks)):
            assert abs(freqs[peaks[a]] - freqs[peaks[b]]) >= spacing


# --- detected_carriers --------------------------------------------------------------------------


def test_detected_carriers_above_threshold_sorted_by_frequency() -> None:
    dbm = np.full(50, -100.0, dtype=np.float32)
    dbm[10], dbm[30], dbm[40] = -60, -50, -92  # last one only 8 dB above floor
    trace = Trace(_freqs(50), dbm, "Max hold")
    got = detected_carriers(trace, floor_dbm=-100.0, threshold_db=10.0)
    assert got == [Carrier(471_000_000, -60.0), Carrier(473_000_000, -50.0)]
    assert isinstance(got[0].freq_hz, int)


def test_detected_carriers_none_in_noise() -> None:
    trace = Trace(_freqs(20), np.full(20, -100.0, dtype=np.float32), "x")
    assert detected_carriers(trace, -100.0, 6.0) == []


def test_find_peaks_spacing_zero_and_positive_agree_on_far_apart_peaks() -> None:
    dbm = np.full(50, -100.0, dtype=np.float32)
    dbm[[5, 20, 40]] = [-50, -60, -70]
    assert find_peaks(_freqs(50), dbm, 10, 0) == find_peaks(_freqs(50), dbm, 10, 100_000)


# --- hardening ----------------------------------------------------------------------------------


def test_published_arrays_are_read_only_and_average_window_is_private() -> None:
    ts = TraceSet(average_count=2)
    ts.update(_sweep([-90.0, -80.0]))
    for tr in (ts.live, ts.max_hold, ts.average, ts.min_hold):
        assert tr is not None
        assert not tr.dbm.flags.writeable and not tr.freqs_hz.flags.writeable
    ts.update(_sweep([-70.0, -60.0]))
    assert ts.average is not None
    np.testing.assert_allclose(ts.average.dbm, [-80.0, -70.0])


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_update_rejects_non_finite(bad: float) -> None:
    ts = TraceSet()
    with pytest.raises(ValueError):
        ts.update(_sweep([-90.0, bad]))
    assert ts.live is None


@given(hnp.arrays(np.float64, st.integers(1, 12), elements=st.integers(-5, 5).map(float)))
def test_prominence_matches_brute_force(arr: npt.NDArray[np.float64]) -> None:
    # Adjacent equal samples are collapsed (as find_peaks does) before computing prominence.
    keep = np.concatenate(([True], np.diff(arr) != 0))
    v = arr[keep].tolist()
    n = len(v)
    expected = []
    for i in range(n):
        lo = hi = -np.inf  # a side with no samples is ignored
        if i > 0:
            j = i - 1
            while j >= 0 and v[j] <= v[i]:
                j -= 1
            lo = min(v[max(j, 0) : i + 1])
        if i < n - 1:
            j = i + 1
            while j < n and v[j] <= v[i]:
                j += 1
            hi = min(v[i : min(j, n - 1) + 1])
        expected.append(v[i] - max(lo, hi))
    assert _prominence(v) == expected


# --- performance --------------------------------------------------------------------------------


def test_peak_detection_scales_on_noisy_50k_trace() -> None:
    import time

    rng = np.random.default_rng(0)
    n = 50_000
    trace = Trace(_freqs(n, step=10e3), rng.normal(-100, 3, n).astype(np.float32), "noisy")
    t0 = time.perf_counter()
    find_peaks(trace.freqs_hz, trace.dbm, 3.0, 0)
    find_peaks(trace.freqs_hz, trace.dbm, 3.0, 50e3)
    detected_carriers(trace, -100.0, 6.0)
    assert time.perf_counter() - t0 < 3.0  # generous: ~0.1 s expected; guards O(n^2) regressions
