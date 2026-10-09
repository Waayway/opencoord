"""Segmented scanning: build a wide, fine-resolution trace from narrow device spans.

The RF Explorer's resolution bandwidth grows with the span, so a 470-960 MHz single sweep is far
too coarse to see a wireless mic. ``SegmentedScanner`` splits the range into overlapping segments,
retunes the link to each in turn, throws away the first sweep after every retune, max-holds the
next N sweeps and stitches the segments into one ``Trace(label="scan")``.

It is driven by ``step()``, which never blocks: it drains whatever sweeps the link has queued,
advances the state machine and returns a ``ScanProgress`` snapshot. The UI calls it once per
frame; a CLI or worker thread loops on it with a short sleep. While a scan runs it owns the link
(it consumes ``link.sweeps``). When it finishes or is cancelled it puts the device back to the
sweep-point count and span it found.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from opencoord.core.types import DeviceConfig, Sweep, Trace
from opencoord.device.link_api import Link

log = logging.getLogger(__name__)

_KHZ = 1_000
_MHZ = 1_000_000
#: Overlap between neighbouring segments, in device steps. Two steps, so the device truncating its
#: step (``C2-F`` works in whole kHz) can never open a gap at a segment edge.
OVERLAP_STEPS = 2
DEFAULT_SETTLE_TIMEOUT_S = 10.0


class Resolution(enum.StrEnum):
    FAST = "fast"
    NORMAL = "normal"
    FINE = "fine"


@dataclass(frozen=True)
class ScanPreset:
    """How one resolution scans: segment width, sweeps max-held per segment, points per sweep.

    ``sweeps_per_s`` is the sweep rate measured on the WSUB1G+ at that span and point count; it
    only feeds ``estimate_seconds()``. The bin width is ``segment_span_hz / (sweep_points - 1)``.
    """

    segment_span_hz: int
    sweeps_per_segment: int
    sweep_points: int
    sweeps_per_s: float


# Measured on the WSUB1G+ Slim (fw 03.39, 500000 baud), sweeps/s by points and span; RBW follows
# the step (the device picks it), so equal-RBW settings can be compared directly:
#   112 pts:  2 MHz 1.93 (RBW 32k) | 5 MHz 2.44 (48k) | 10-50 MHz 3.35 (110k-510k) | 100 MHz 1.71
#   512 pts:  2 MHz 0.27 (5k) | 10 MHz 0.47 (32k) | 20 MHz 0.61 (48k) | 30 MHz 0.72 (64k)
#             40-200 MHz 0.89 (95k-510k)
#   1024 pts: 40 MHz 0.31 (48k) | 50 MHz 0.36 (64k) | 100-200 MHz 0.45 (110k-225k)
# At equal RBW, 512 points over a 4-5x wider span covers as many MHz/s as 112 points or more, with
# 4-5x fewer retunes, so Normal and Fine use 512 points. The first sweep after a retune arrives
# about one sweep period after set_span (confirmation takes 30-70 ms), so a segment costs about
# (sweeps_per_segment + 1) sweep periods.
PRESETS: dict[Resolution, ScanPreset] = {
    # 180 kHz bins (RBW 225 kHz); 470-960 MHz in 25 segments, about 15 s.
    Resolution.FAST: ScanPreset(20 * _MHZ, 1, 112, 3.35),
    # 78 kHz bins (RBW 95 kHz); 470-960 MHz in 13 segments, about 58 s.
    Resolution.NORMAL: ScanPreset(40 * _MHZ, 3, 512, 0.89),
    # 39 kHz bins (RBW 48 kHz); 470-960 MHz in 25 segments, about 2 min.
    Resolution.FINE: ScanPreset(20 * _MHZ, 2, 512, 0.61),
}
#: Sweep rate assumed for an overview (one wide sweep at the device's current points; 112 points
#: over 100 MHz measured 1.71/s).
OVERVIEW_SWEEPS_PER_S = 1.7


@dataclass(frozen=True)
class Segment:
    start_hz: int
    stop_hz: int


@dataclass(frozen=True, eq=False)
class ScanProgress:
    """Snapshot returned by ``step()``; ``partial`` is the stitched trace of finished segments."""

    segment_index: int
    segment_count: int
    fraction: float
    partial: Trace | None
    done: bool


def plan_segments(start_hz: int, stop_hz: int, span_hz: int, points: int) -> list[Segment]:
    """Segments of ``span_hz`` covering ``start_hz..stop_hz``, overlapping by >= 2 steps.

    Edges are whole kHz (``start`` rounded down, ``stop`` up), because the device is tuned in kHz.
    The last segment is shifted to end exactly at ``stop`` so every segment has the same step.
    """
    start = start_hz // _KHZ * _KHZ
    stop = -(-stop_hz // _KHZ) * _KHZ
    if stop <= start:
        raise ValueError("stop must be greater than start")
    span = max(span_hz // _KHZ * _KHZ, _KHZ)
    if stop - start <= span:
        return [Segment(start, stop)]
    overlap = math.ceil(OVERLAP_STEPS * span / (points - 1) / _KHZ) * _KHZ
    advance = span - overlap
    if advance <= 0:
        raise ValueError("segment span too small for its point count")
    starts: list[int] = []
    s = start
    while s + span < stop:
        starts.append(s)
        s += advance
    starts.append(stop - span)
    return [Segment(a, a + span) for a in starts]


def stitch(traces: Sequence[Trace]) -> Trace:
    """Merge segment traces onto one Hz grid (the finest step), keeping the max where they overlap.

    Each point goes to the nearest grid bin; bins no point fell into are left out.
    """
    if not traces:
        raise ValueError("nothing to stitch")
    if len(traces) == 1:
        only = traces[0]
        return Trace(only.freqs_hz.copy(), only.dbm.copy(), "scan")
    steps = [float(t.freqs_hz[-1] - t.freqs_hz[0]) / (len(t.freqs_hz) - 1) for t in traces]
    step = min(steps)
    origin = min(float(t.freqs_hz[0]) for t in traces)
    bins = [np.rint((t.freqs_hz - origin) / step).astype(np.int64) for t in traces]
    size = max(int(np.max(b)) for b in bins) + 1
    held = np.full(size, -np.inf, dtype=np.float64)
    for b, t in zip(bins, traces, strict=True):
        np.maximum.at(held, b, t.dbm.astype(np.float64))
    covered = np.flatnonzero(np.isfinite(held))
    freqs: npt.NDArray[np.float64] = origin + covered.astype(np.float64) * step
    return Trace(freqs, held[covered].astype(np.float32), "scan")


class SegmentedScanner:
    """Scan ``start_hz..stop_hz`` segment by segment on an open ``link``; see the module doc."""

    def __init__(
        self,
        link: Link,
        start_hz: int,
        stop_hz: int,
        resolution: Resolution = Resolution.NORMAL,
        *,
        settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        _overview: bool = False,
    ) -> None:
        if stop_hz <= start_hz:
            raise ValueError("stop must be greater than start")
        caps, config = link.capabilities, link.config
        if not link.is_open or caps is None or config is None:
            raise RuntimeError("the device is not connected")
        start, stop = max(start_hz, caps.min_hz), min(stop_hz, caps.max_hz)
        if stop <= start:
            raise ValueError("the range is outside what the device can tune")
        self._link = link
        self._clock = clock
        self._settle_timeout = settle_timeout_s
        if _overview:
            self._points = config.sweep_points
            self._sweeps_per_segment = 1
            self._sweeps_per_s = OVERVIEW_SWEEPS_PER_S
            self._segments = [Segment(start, min(stop, start + caps.max_span_hz))]
        else:
            preset = PRESETS[resolution]
            self._points = preset.sweep_points
            self._sweeps_per_segment = preset.sweeps_per_segment
            self._sweeps_per_s = preset.sweeps_per_s
            self._segments = plan_segments(start, stop, preset.segment_span_hz, self._points)
        self._original: DeviceConfig | None = None
        self._started = False
        self._finished = False
        self._index = 0
        self._discarded = False
        self._collected: list[Sweep] = []
        self._held: list[Trace] = []
        self._partial: Trace | None = None
        self._result: Trace | None = None
        self._last_progress = 0.0

    @classmethod
    def overview(
        cls,
        link: Link,
        start_hz: int,
        stop_hz: int,
        *,
        settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> SegmentedScanner:
        """One sweep over the range (clamped to the device's max span) at the current points."""
        return cls(
            link, start_hz, stop_hz, settle_timeout_s=settle_timeout_s, clock=clock, _overview=True
        )

    @property
    def segments(self) -> list[Segment]:
        return list(self._segments)

    @property
    def result(self) -> Trace | None:
        """The stitched trace once the scan has completed (``None`` if cancelled)."""
        return self._result

    def estimate_seconds(self) -> float:
        """Expected duration: each segment costs its sweeps plus the discarded first one."""
        return len(self._segments) * (self._sweeps_per_segment + 1) / self._sweeps_per_s

    def cancel(self) -> None:
        """Stop scanning and restore the device; the next ``step()`` reports ``done``."""
        if not self._finished:
            self._finish()

    def step(self) -> ScanProgress:
        """Drain queued sweeps, advance the scan and report progress. Never blocks."""
        if self._finished:
            return self._progress()
        if not self._started:
            self._started = True
            self._original = self._link.config
            self._request()
        while not self._finished:
            try:
                sweep = self._link.sweeps.get_nowait()
            except queue.Empty:
                break
            self._accept(sweep)
        if not self._finished and self._clock() - self._last_progress > self._settle_timeout:
            seg = self._segments[self._index]
            log.warning(
                "segment %d-%d Hz did not settle in %.0f s, retuning",
                seg.start_hz,
                seg.stop_hz,
                self._settle_timeout,
            )
            self._request()
        return self._progress()

    # --- internals ---

    def _request(self) -> None:
        """Tune to the current segment, dropping sweeps queued before the retune."""
        config = self._link.config
        if config is None or config.sweep_points != self._points:
            self._link.set_sweep_points(self._points)
        seg = self._segments[self._index]
        self._link.set_span(seg.start_hz, seg.stop_hz)
        while True:
            try:
                self._link.sweeps.get_nowait()
            except queue.Empty:
                break
        self._discarded = False
        self._collected = []
        self._last_progress = self._clock()

    def _matches(self, axis_start: int, axis_stop: int, points: int) -> bool:
        seg = self._segments[self._index]
        tol = max(math.ceil((seg.stop_hz - seg.start_hz) / (self._points - 1)), _KHZ)
        return (
            points == self._points
            and abs(axis_start - seg.start_hz) <= tol
            and abs(axis_stop - seg.stop_hz) <= tol
        )

    def _accept(self, sweep: Sweep) -> None:
        cfg = self._link.config
        if cfg is None or not self._matches(cfg.start_hz, cfg.stop_hz, cfg.sweep_points):
            return
        if not self._matches(sweep.start_hz, sweep.stop_hz, len(sweep.dbm)):
            return
        self._last_progress = self._clock()
        if not self._discarded:  # the first sweep after a retune is not trusted
            self._discarded = True
            return
        self._collected.append(sweep)
        if len(self._collected) < self._sweeps_per_segment:
            return
        held = np.max(np.stack([s.dbm for s in self._collected]), axis=0)
        self._held.append(Trace(self._collected[0].freqs_hz, held, "segment"))
        self._partial = stitch(self._held)
        self._collected = []
        if self._index + 1 < len(self._segments):
            self._index += 1
            self._request()
        else:
            self._result = self._partial
            self._finish()

    def _finish(self) -> None:
        """Mark done and put the device back to the points and span it had before the scan."""
        self._finished = True
        original = self._original
        if original is None or not self._link.is_open:
            return
        try:
            if self._points != original.sweep_points:
                self._link.set_sweep_points(original.sweep_points)
            self._link.set_span(original.start_hz, original.stop_hz)
        except (RuntimeError, ValueError):
            log.warning("could not restore the device settings after the scan", exc_info=True)

    def _progress(self) -> ScanProgress:
        count = len(self._segments)
        if self._finished:
            fraction = 1.0 if self._result is not None else self._index / count
            return ScanProgress(self._index, count, fraction, self._partial, True)
        part = len(self._collected) / self._sweeps_per_segment
        return ScanProgress(self._index, count, (self._index + part) / count, self._partial, False)


__all__ = [
    "OVERVIEW_SWEEPS_PER_S",
    "PRESETS",
    "Resolution",
    "ScanPreset",
    "ScanProgress",
    "Segment",
    "SegmentedScanner",
    "plan_segments",
    "stitch",
]
