"""SegmentedScanner: segment plan, stitching and the step() state machine."""

from __future__ import annotations

import queue
import time
from collections.abc import Iterator
from itertools import pairwise

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep, Trace
from opencoord.device import models
from opencoord.device.link_api import Link, LinkEvent
from opencoord.device.models import Capabilities
from opencoord.device.protocol import make_sweep
from opencoord.device.scanner import (
    MAX_RETUNES,
    PRESETS,
    Resolution,
    ScanProgress,
    SegmentedScanner,
    plan_segments,
    stitch,
)
from opencoord.device.simulator import CARRIERS, SimulatedLink

MHZ = 1_000_000


# --- a manually driven link --------------------------------------------------------------------


class ManualLink:
    """A ``Link`` whose device confirms and sweeps only when the test says so."""

    def __init__(
        self,
        *,
        points: int = 112,
        max_span_hz: int | None = None,
        model_code: int = 10,
        span: tuple[int, int] = (431 * MHZ, 441 * MHZ),
    ) -> None:
        self.sweeps: queue.Queue[Sweep] = queue.Queue(maxsize=256)
        self.events: queue.Queue[LinkEvent] = queue.Queue(maxsize=64)
        self.requests: list[tuple[str, int, int]] = []
        self.emitted: list[Sweep] = []
        self._fixed_max_span = max_span_hz
        self._model = ModelInfo(model_code, None, "03.39")
        self._config = self._make(*span, points)
        self._pending: list[tuple[str, int, int]] = []
        self.open_ = True

    def max_span(self, points: int) -> int:
        """Like the WSUB1G+: 959.95 MHz at 112 points, 342.37 MHz at 512."""
        if self._fixed_max_span is not None:
            return self._fixed_max_span
        return min(959_950_000, 342_370_000 * 512 // points)

    def _make(self, start: int, stop: int, points: int) -> DeviceConfig:
        start = start // 1000 * 1000  # C2-F is in kHz; the device truncates the step
        stop = min(stop, start + self.max_span(points))
        return DeviceConfig(
            start_hz=start,
            step_hz=(stop - start) // (points - 1),
            amp_top_dbm=-10.0,
            amp_bottom_dbm=-120.0,
            sweep_points=points,
            expansion_active=False,
            mode=0,
            min_hz=50_000,
            max_hz=960 * MHZ,
            max_span_hz=self.max_span(points),
            rbw_hz=None,
            amp_offset_db=0.0,
            calculator_mode=0,
        )

    @property
    def model(self) -> ModelInfo:
        return self._model

    @property
    def config(self) -> DeviceConfig:
        return self._config

    @property
    def capabilities(self) -> Capabilities:
        return models.resolve(self._model, self._config)

    @property
    def is_open(self) -> bool:
        return self.open_

    def open(self) -> None:
        self.open_ = True

    def close(self) -> None:
        self.open_ = False

    def set_span(self, start_hz: int, stop_hz: int) -> None:
        # Clamped against the capabilities at call time, like the real links.
        stop_hz = min(stop_hz, start_hz + self.capabilities.max_span_hz)
        self.requests.append(("span", start_hz, stop_hz))
        self._pending.append(("span", start_hz, stop_hz))

    def set_sweep_points(self, points: int) -> None:
        self.requests.append(("points", points, 0))
        self._pending.append(("points", points, 0))

    def hold(self) -> None:
        pass

    def switch_module(self, main: bool) -> None:
        pass

    # --- test controls ---

    def confirm(self) -> None:
        """The device applies every pending command."""
        for kind, a, b in self._pending:
            cfg = self._config
            if kind == "points":
                self._config = self._make(cfg.start_hz, cfg.stop_hz, a)
            else:
                self._config = self._make(a, b, cfg.sweep_points)
        self._pending.clear()

    def emit(self, level: float = -100.0, peak_hz: int | None = None, peak: float = -40.0) -> Sweep:
        cfg = self._config
        samples = np.full(cfg.sweep_points, level, dtype=np.float32)
        if peak_hz is not None:
            freqs = cfg.start_hz + np.arange(cfg.sweep_points) * cfg.step_hz
            i = int(np.argmin(np.abs(freqs - peak_hz)))
            if abs(freqs[i] - peak_hz) <= cfg.step_hz:
                samples[i] = peak
        sweep = make_sweep(cfg, samples, time.time())
        self.sweeps.put_nowait(sweep)
        self.emitted.append(sweep)
        return sweep


def test_manual_link_is_a_link() -> None:
    link: Link = ManualLink()
    assert link.is_open


def drive(link: ManualLink, scanner: SegmentedScanner, sweeps: int) -> ScanProgress:
    """Confirm and feed ``sweeps`` sweeps per step until the scan (and restore) is done."""
    p = scanner.step()
    for _ in range(1000):
        if p.done:
            return p
        link.confirm()
        for _ in range(sweeps):
            link.emit()
        p = scanner.step()
    raise AssertionError(f"scan never finished: {p}")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- segment plan ------------------------------------------------------------------------------


def test_plan_single_segment_when_range_fits() -> None:
    segs = plan_segments(470 * MHZ, 480 * MHZ, 20 * MHZ, 112)
    assert [(s.start_hz, s.stop_hz) for s in segs] == [(470 * MHZ, 480 * MHZ)]


def test_plan_470_960_normal() -> None:
    preset = PRESETS[Resolution.NORMAL]
    segs = plan_segments(470 * MHZ, 960 * MHZ, preset.segment_span_hz, preset.sweep_points)
    assert segs[0].start_hz == 470 * MHZ and segs[-1].stop_hz == 960 * MHZ
    assert all(s.stop_hz - s.start_hz == preset.segment_span_hz for s in segs)


@given(
    start_khz=st.integers(50, 900_000),
    width_khz=st.integers(1_000, 500_000),
    span_mhz=st.sampled_from([5, 10, 20, 40, 50]),
    points=st.sampled_from([112, 512, 1024]),
)
def test_plan_covers_range_with_overlap(
    start_khz: int, width_khz: int, span_mhz: int, points: int
) -> None:
    start, stop = start_khz * 1000, (start_khz + width_khz) * 1000
    span = span_mhz * MHZ
    segs = plan_segments(start, stop, span, points)
    assert segs[0].start_hz == start and segs[-1].stop_hz == stop
    step = span / (points - 1)
    for s in segs:
        assert s.start_hz % 1000 == 0 and s.stop_hz % 1000 == 0
        assert 0 < s.stop_hz - s.start_hz <= span
    for a, b in pairwise(segs):
        assert a.start_hz < b.start_hz
        assert a.stop_hz - b.start_hz >= 2 * step  # overlap: no gap even if the device truncates


# --- stitching ---------------------------------------------------------------------------------


def _trace(start: int, step: int, dbm: list[float]) -> Trace:
    freqs = start + np.arange(len(dbm), dtype=np.float64) * step
    return Trace(freqs, np.asarray(dbm, dtype=np.float32), "segment")


def test_stitch_concatenates_and_keeps_max_in_overlap() -> None:
    a = _trace(100_000, 1000, [-100, -90, -80, -70])  # 100..103 kHz
    b = _trace(102_000, 1000, [-60, -95, -100])  # 102..104 kHz
    out = stitch([a, b])
    assert out.label == "scan"
    assert list(out.freqs_hz) == [100_000, 101_000, 102_000, 103_000, 104_000]
    assert list(out.dbm) == [-100, -90, -60, -70, -100]
    assert out.dbm.dtype == np.float32 and out.freqs_hz.dtype == np.float64


def test_stitch_keeps_measured_frequencies_of_offset_axes() -> None:
    a = _trace(0, 1000, [-100] * 10)  # 0..9000
    b = _trace(8_300, 1000, [-50] * 10)  # 8300..17300, not on a's grid
    out = stitch([b, a])  # order does not matter
    assert list(out.freqs_hz) == [*range(0, 10_000, 1000), *range(10_300, 18_000, 1000)]
    assert float(out.dbm[8]) == -50 and float(out.dbm[9]) == -50  # overlap merged, max kept
    assert float(out.dbm[7]) == -100


def test_stitch_single_trace_is_unchanged() -> None:
    a = _trace(470 * MHZ, 90_090, [-100, -50, -100])
    out = stitch([a])
    assert np.array_equal(out.freqs_hz, a.freqs_hz) and np.array_equal(out.dbm, a.dbm)


def test_stitch_needs_input() -> None:
    with pytest.raises(ValueError):
        stitch([])


# --- the scanner on a manual link ---------------------------------------------------------------


def test_step_is_non_blocking_and_ignores_stale_sweeps() -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 470 * MHZ, 480 * MHZ, Resolution.FAST)
    t0 = time.monotonic()
    p = scanner.step()
    assert time.monotonic() - t0 < 0.05
    assert not p.done and p.segment_index == 0 and p.segment_count == 1 and p.partial is None
    assert link.requests == [("span", 470 * MHZ, 480 * MHZ)]
    link.emit(level=0.0)  # old config (431-441 MHz): must be ignored
    p = scanner.step()
    assert p.fraction == 0.0 and not p.done


def test_discards_first_sweep_after_reconfig_then_max_holds_n() -> None:
    link = ManualLink()
    preset = PRESETS[Resolution.FAST]
    scanner = SegmentedScanner(link, 470 * MHZ, 480 * MHZ, Resolution.FAST)
    scanner.step()
    link.confirm()
    link.emit(level=0.0)  # first sweep after the reconfig: discarded
    p = scanner.step()
    assert p.fraction == 0.0
    for i in range(preset.sweeps_per_segment):
        link.emit(level=-100.0, peak_hz=475 * MHZ, peak=-50.0 - i)
    p = scanner.step()
    assert not p.done and p.fraction == 1.0  # restoring the device's span
    result = scanner.result
    assert result is not None and p.partial is result
    link.confirm()
    assert scanner.step().done
    assert float(result.dbm.max()) == -50.0  # the 0 dBm discarded sweep never shows
    assert result.start_hz == 470 * MHZ and abs(result.stop_hz - 480 * MHZ) < 200_000
    assert len(result.dbm) == preset.sweep_points


def test_config_must_match_before_sweeps_count() -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 470 * MHZ, 480 * MHZ, Resolution.FAST)
    scanner.step()
    # A sweep that happens to have the right axis but arrives while config is still old.
    target = link._make(470 * MHZ, 480 * MHZ, 112)
    link.sweeps.put_nowait(make_sweep(target, np.zeros(112, dtype=np.float32), 0.0))
    link.sweeps.put_nowait(make_sweep(target, np.zeros(112, dtype=np.float32), 0.0))
    p = scanner.step()
    assert p.fraction == 0.0


def test_segments_advance_left_to_right_with_partial_traces() -> None:
    link = ManualLink()
    preset = PRESETS[Resolution.FAST]
    scanner = SegmentedScanner(link, 470 * MHZ, 530 * MHZ, Resolution.FAST)
    count = len(scanner.segments)
    assert count >= 3
    stops: list[int] = []
    fractions: list[float] = []
    p = scanner.step()
    while scanner.result is None:
        link.confirm()
        for _ in range(preset.sweeps_per_segment + 1):
            link.emit()
        p = scanner.step()
        fractions.append(p.fraction)
        assert p.partial is not None
        stops.append(p.partial.stop_hz)
    assert len(stops) == count
    assert stops == sorted(stops) and stops[0] < stops[-1]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert [r[1] for r in link.requests[:count]] == [s.start_hz for s in scanner.segments]


def test_old_axis_sweep_after_next_segment_is_confirmed_is_ignored() -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 470 * MHZ, 530 * MHZ, Resolution.FAST)
    scanner.step()
    link.confirm()
    link.emit()  # discarded
    stale = link.emit()  # collected: segment 0 done, segment 1 requested
    p = scanner.step()
    assert p.segment_index == 1
    link.confirm()
    link.sweeps.put_nowait(stale)  # a segment-0 sweep arriving late
    link.emit(level=0.0)  # first segment-1 sweep: discarded
    p = scanner.step()
    assert p.segment_index == 1 and p.fraction == pytest.approx(1 / p.segment_count)
    link.emit()
    p = scanner.step()
    assert p.segment_index == 2  # stale sweep did not count as segment 1's discard or data
    partial = p.partial
    assert partial is not None and float(partial.dbm.max()) < -50


def test_changes_sweep_points_and_restores_the_device_afterwards() -> None:
    link = ManualLink()
    original = link.config
    preset = PRESETS[Resolution.NORMAL]
    assert preset.sweep_points != 112
    scanner = SegmentedScanner(link, 470 * MHZ, 500 * MHZ, Resolution.NORMAL)
    p = scanner.step()
    assert link.requests[0] == ("points", preset.sweep_points, 0)
    while scanner.result is None:
        link.confirm()
        for _ in range(preset.sweeps_per_segment + 1):
            link.emit()
        p = scanner.step()
    # Restore is phased: points first, the span only once the points are confirmed.
    assert not p.done and link.requests[-1] == ("points", 112, 0)
    assert not scanner.step().done
    link.confirm()
    p = scanner.step()
    assert not p.done and link.requests[-1] == ("span", original.start_hz, original.stop_hz)
    link.confirm()
    assert scanner.step().done
    assert link.config == original


def test_restores_a_span_wider_than_the_scan_points_allow() -> None:
    # Found at 470-960 MHz / 112 points; at 512 points the max span is only 342 MHz.
    link = ManualLink(span=(470 * MHZ, 960 * MHZ))
    original = link.config
    scanner = SegmentedScanner(link, 600 * MHZ, 650 * MHZ, Resolution.NORMAL)
    p = drive(link, scanner, PRESETS[Resolution.NORMAL].sweeps_per_segment + 1)
    assert p.done and scanner.result is not None
    assert link.config == original
    assert link.config.stop_hz > 950 * MHZ


def test_preset_points_are_clamped_to_the_device_keeping_the_bin_width() -> None:
    link = ManualLink(model_code=3)  # WSUB1G: 112 points max
    assert link.capabilities.sweep_points_max == 112
    preset = PRESETS[Resolution.NORMAL]
    scanner = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, Resolution.NORMAL)
    assert scanner.sweep_points == 112
    bin_hz = preset.segment_span_hz / (preset.sweep_points - 1)
    seg = scanner.segments[0]
    assert (seg.stop_hz - seg.start_hz) / 111 == pytest.approx(bin_hz, rel=0.01)
    p = drive(link, scanner, preset.sweeps_per_segment + 1)
    assert p.done and scanner.result is not None
    assert not any(r[0] == "points" for r in link.requests)
    assert scanner.estimate_seconds() > 0


def test_cancel_restores_and_finishes() -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, Resolution.FAST)
    scanner.step()
    scanner.cancel()
    assert link.requests[-1][0:2] == ("span", 431 * MHZ)
    assert not scanner.step().done
    link.confirm()
    p = scanner.step()
    assert p.done and scanner.result is None and not p.stalled
    n = len(link.requests)
    scanner.cancel()
    scanner.step()
    assert len(link.requests) == n  # restores once


def test_rerequests_a_segment_that_does_not_settle_then_stalls() -> None:
    link = ManualLink()
    clock = Clock()
    scanner = SegmentedScanner(
        link, 470 * MHZ, 480 * MHZ, Resolution.FAST, settle_timeout_s=5.0, clock=clock
    )
    scanner.step()
    clock.t = 4.9
    scanner.step()
    assert len(link.requests) == 1
    clock.t = 5.1
    scanner.step()
    assert link.requests == [("span", 470 * MHZ, 480 * MHZ)] * 2
    for _ in range(MAX_RETUNES):
        clock.t += 5.1
        p = scanner.step()
    assert p.stalled and not p.done
    assert link.requests[-1][0:2] == ("span", 431 * MHZ)  # restoring
    assert link.requests.count(("span", 470 * MHZ, 480 * MHZ)) == 1 + MAX_RETUNES
    link.confirm()
    p = scanner.step()
    assert p.done and p.stalled and scanner.result is None


def test_overview_is_one_clamped_sweep_at_current_points() -> None:
    link = ManualLink(max_span_hz=300 * MHZ)
    scanner = SegmentedScanner.overview(link, 470 * MHZ, 960 * MHZ)
    assert scanner.range_hz == (470 * MHZ, 770 * MHZ)
    assert [(s.start_hz, s.stop_hz) for s in scanner.segments] == [(470 * MHZ, 770 * MHZ)]
    p = scanner.step()
    assert link.requests == [("span", 470 * MHZ, 770 * MHZ)]  # no sweep-point change
    link.confirm()
    link.emit(level=0.0)  # discarded
    link.emit(level=-90.0)
    scanner.step()
    link.confirm()
    p = scanner.step()
    assert p.done and scanner.result is not None
    assert len(scanner.result.dbm) == 112 and float(scanner.result.dbm.max()) == -90.0
    assert scanner.estimate_seconds() > 0


def test_estimate_seconds_from_preset() -> None:
    link = ManualLink()
    for res in Resolution:
        preset = PRESETS[res]
        scanner = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, res)
        expected = len(scanner.segments) * (preset.sweeps_per_segment + 1) / preset.sweeps_per_s
        assert scanner.estimate_seconds() == pytest.approx(expected)
    normal = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, Resolution.NORMAL).estimate_seconds()
    fast = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, Resolution.FAST).estimate_seconds()
    fine = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, Resolution.FINE).estimate_seconds()
    assert fast < normal < fine
    assert 45 <= normal <= 75  # plan Q7: Normal 470-960 MHz in about 60 s


def test_range_is_clamped_to_the_device_and_validated() -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 900 * MHZ, 2000 * MHZ, Resolution.FAST)
    assert scanner.segments[-1].stop_hz == 960 * MHZ
    with pytest.raises(ValueError):
        SegmentedScanner(link, 500 * MHZ, 400 * MHZ, Resolution.FAST)
    link.close()
    with pytest.raises(RuntimeError):
        SegmentedScanner(link, 470 * MHZ, 480 * MHZ, Resolution.FAST)


@pytest.mark.parametrize("resolution", list(Resolution))
def test_stitched_frequencies_are_measured_frequencies(resolution: Resolution) -> None:
    link = ManualLink()
    scanner = SegmentedScanner(link, 470 * MHZ, 960 * MHZ, resolution)
    drive(link, scanner, PRESETS[resolution].sweeps_per_segment + 1)
    result = scanner.result
    assert result is not None
    measured = np.unique(np.concatenate([s.freqs_hz for s in link.emitted]))
    i = np.clip(np.searchsorted(measured, result.freqs_hz), 1, len(measured) - 1)
    err = np.minimum(
        np.abs(measured[i] - result.freqs_hz), np.abs(measured[i - 1] - result.freqs_hz)
    )
    assert float(err.max()) < 1000  # exact in fact; 1 kHz is the device's tuning granularity
    assert np.all(np.diff(result.freqs_hz) > 0)
    step = PRESETS[resolution].segment_span_hz / (PRESETS[resolution].sweep_points - 1)
    assert float(np.max(np.diff(result.freqs_hz))) < 1.5 * step  # no gaps at segment joins
    assert result.start_hz == 470 * MHZ and abs(result.stop_hz - 960 * MHZ) <= step


# --- end to end against the simulator ----------------------------------------------------------


@pytest.fixture
def sim() -> Iterator[SimulatedLink]:
    link = SimulatedLink(seed=5, sweep_interval_s=0.002)
    link.open()
    yield link
    link.close()


def _run(scanner: SegmentedScanner, timeout_s: float = 20.0) -> list[ScanProgress]:
    out: list[ScanProgress] = []
    deadline = time.monotonic() + timeout_s
    while True:
        p = scanner.step()
        out.append(p)
        if p.done:
            return out
        assert time.monotonic() < deadline, f"scan stuck at {p}"
        time.sleep(0.001)


@pytest.mark.parametrize("resolution", list(Resolution))
def test_simulated_scan_finds_the_carriers(sim: SimulatedLink, resolution: Resolution) -> None:
    original = sim.config
    assert original is not None
    scanner = SegmentedScanner(sim, 555 * MHZ, 680 * MHZ, resolution)
    progress = _run(scanner)
    result = scanner.result
    assert result is not None and progress[-1].partial is result
    assert result.start_hz == 555 * MHZ and abs(result.stop_hz - 680 * MHZ) < 100_000
    assert np.all(np.diff(result.freqs_hz) > 0)
    preset = PRESETS[resolution]
    bin_hz = float(np.median(np.diff(result.freqs_hz)))
    assert bin_hz == pytest.approx(preset.segment_span_hz / (preset.sweep_points - 1), rel=0.01)
    assert float(np.max(np.diff(result.freqs_hz))) < 1.5 * bin_hz  # no gaps
    for centre, _level in CARRIERS:
        if 555 * MHZ < centre < 680 * MHZ:
            near = np.abs(result.freqs_hz - centre) < 2 * bin_hz
            assert float(result.dbm[near].max()) > -75, centre
    # the device ends up where it was
    deadline = time.monotonic() + 2
    while sim.config != original:
        assert time.monotonic() < deadline, sim.config
        time.sleep(0.005)
