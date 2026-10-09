"""Segmented scanning: build a wide, fine-resolution trace from narrow device spans.

The RF Explorer's resolution bandwidth grows with the span, so a 470-960 MHz single sweep is far
too coarse to see a wireless mic. ``SegmentedScanner`` splits the range into overlapping segments,
retunes the link to each in turn, throws away the first sweep after every retune, max-holds the
next N sweeps and stitches the segments into one ``Trace(label="scan")``.

It is driven by ``step()``, which never blocks: it drains whatever sweeps the link has queued,
advances the state machine and returns a ``ScanProgress`` snapshot. The UI calls it once per
frame; a CLI or worker thread loops on it with a short sleep. While a scan runs it owns the link
(it consumes ``link.sweeps``). When it finishes or is cancelled it puts the device back to the
sweep-point count and span it found: first the points (which change the device's max span), then
the span, each confirmed before ``step()`` reports ``done``.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from opencoord.core.types import DeviceConfig, Sweep, Trace
from opencoord.device.link_api import Link

log = logging.getLogger(__name__)

_KHZ = 1_000
_MHZ = 1_000_000
#: Overlap between neighbouring segments, in device steps. Two steps, so the device truncating its
#: step (``C2-F`` works in whole kHz) can never open a gap at a segment edge.
OVERLAP_STEPS = 2
DEFAULT_SETTLE_TIMEOUT_S = 10.0
#: Retunes of a segment that does not settle before the scan is abandoned as stalled.
MAX_RETUNES = 3


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
#: Fastest sweep rate measured (112 points); caps the estimate when a preset's points are reduced.
_MAX_SWEEPS_PER_S = 3.35


@dataclass(frozen=True)
class Segment:
    start_hz: int
    stop_hz: int


@dataclass(frozen=True, eq=False)
class ScanProgress:
    """Snapshot returned by ``step()``; ``partial`` is the stitched trace of finished segments.

    ``done`` becomes true only once the device is back at the settings it had before the scan (or
    restoring gave up). ``stalled`` means a segment never settled after ``MAX_RETUNES`` retunes;
    the scan was then abandoned (``result`` stays ``None``).
    """

    segment_index: int
    segment_count: int
    fraction: float
    partial: Trace | None
    done: bool
    stalled: bool = False


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
    """Join segment traces left to right, keeping every point's own frequency.

    A point of a later segment that lies within half a step of the joined trace so far (the
    overlap) is merged into the nearest existing point, keeping the max level; the others are
    appended. So every output frequency is a frequency the device actually measured.
    """
    if not traces:
        raise ValueError("nothing to stitch")
    ordered = sorted(traces, key=lambda t: float(t.freqs_hz[0]))
    freqs = ordered[0].freqs_hz.astype(np.float64)
    dbm = ordered[0].dbm.astype(np.float32)
    for t in ordered[1:]:
        tail_step = float(freqs[-1] - freqs[-2]) if len(freqs) > 1 else 0.0
        overlap = t.freqs_hz <= freqs[-1] + tail_step / 2
        if overlap.any():
            x = t.freqs_hz[overlap]
            right = np.clip(np.searchsorted(freqs, x), 0, len(freqs) - 1)
            left = np.clip(right - 1, 0, len(freqs) - 1)
            nearest = np.where(np.abs(freqs[left] - x) <= np.abs(freqs[right] - x), left, right)
            np.maximum.at(dbm, nearest, t.dbm[overlap].astype(np.float32))
        freqs = np.concatenate([freqs, t.freqs_hz[~overlap].astype(np.float64)])
        dbm = np.concatenate([dbm, t.dbm[~overlap].astype(np.float32)])
    return Trace(freqs, dbm, "scan")


_Phase = Literal["idle", "scanning", "restore_points", "restore_span", "done"]


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
            span, rate = preset.segment_span_hz, preset.sweeps_per_s
            points = min(preset.sweep_points, max(caps.sweep_points_max, config.sweep_points))
            if points != preset.sweep_points:
                # The device cannot do the preset's points: keep the bin width with a narrower
                # segment. Sweep time scales roughly with points, capped at the fastest measured.
                span = round(preset.segment_span_hz * (points - 1) / (preset.sweep_points - 1))
                rate = min(rate * preset.sweep_points / points, _MAX_SWEEPS_PER_S)
            self._points = points
            self._sweeps_per_segment = preset.sweeps_per_segment
            self._sweeps_per_s = rate
            self._segments = plan_segments(start, stop, span, points)
        self._original: DeviceConfig | None = None
        self._phase: _Phase = "idle"
        self._stalled = False
        self._retunes = 0
        self._index = 0
        self._discarded = False
        self._collected: list[Sweep] = []
        self._held: list[Trace] = []
        self._partial: Trace | None = None
        self._result: Trace | None = None
        self._last_progress = 0.0
        self._restore_from: DeviceConfig | None = None

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
    def range_hz(self) -> tuple[int, int]:
        """The range actually scanned, after clamping to the device (and, for an overview, to
        its max span)."""
        return self._segments[0].start_hz, self._segments[-1].stop_hz

    @property
    def sweep_points(self) -> int:
        """Points per sweep used for the scan (the preset's, clamped to the device)."""
        return self._points

    @property
    def result(self) -> Trace | None:
        """The stitched trace once every segment is in (``None`` if cancelled or stalled)."""
        return self._result

    def estimate_seconds(self) -> float:
        """Expected duration: each segment costs its sweeps plus the discarded first one."""
        return len(self._segments) * (self._sweeps_per_segment + 1) / self._sweeps_per_s

    def cancel(self) -> None:
        """Stop scanning; ``step()`` then restores the device and reports ``done``."""
        if self._phase in ("idle", "scanning"):
            self._begin_restore()

    def step(self) -> ScanProgress:
        """Drain queued sweeps, advance the scan (or the restore) and report progress.

        Never blocks. Keep calling it until ``done``: after the last segment it still has to put
        the device back (sweep points first, then the span, each confirmed by the device).
        """
        if self._phase == "idle":
            self._phase = "scanning"
            self._original = self._link.config
            self._request()
        while self._phase != "done":
            try:
                sweep = self._link.sweeps.get_nowait()
            except queue.Empty:
                break
            if self._phase == "scanning":
                self._accept(sweep)
        if self._phase == "scanning":
            self._check_settled()
        elif self._phase != "done":
            self._advance_restore()
        return self._progress()

    # --- scanning ---

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

    def _check_settled(self) -> None:
        if self._clock() - self._last_progress <= self._settle_timeout:
            return
        seg = self._segments[self._index]
        if self._retunes >= MAX_RETUNES:
            log.warning("segment %d-%d Hz never settled; scan stalled", seg.start_hz, seg.stop_hz)
            self._stalled = True
            self._begin_restore()
            return
        self._retunes += 1
        log.warning(
            "segment %d-%d Hz did not settle in %.0f s, retuning",
            seg.start_hz,
            seg.stop_hz,
            self._settle_timeout,
        )
        self._request()

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
        self._retunes = 0
        if self._index + 1 < len(self._segments):
            self._index += 1
            self._request()
        else:
            self._result = self._partial
            self._begin_restore()

    # --- restoring the device ---

    def _begin_restore(self) -> None:
        """Put the device back: sweep points first (that changes the max span), then the span."""
        original = self._original
        self._last_progress = self._clock()
        if original is None or not self._link.is_open:
            self._phase = "done"
            return
        config = self._link.config
        # Configs are replaced on every confirmation, so a restore step counts only once the
        # config object differs from the one current when its command was sent (earlier queued
        # retunes may still be confirmed in between).
        self._restore_from = config
        try:
            if self._points != original.sweep_points or (
                config is not None and config.sweep_points != original.sweep_points
            ):
                self._link.set_sweep_points(original.sweep_points)
                self._phase = "restore_points"
            else:
                self._link.set_span(original.start_hz, original.stop_hz)
                self._phase = "restore_span"
        except (RuntimeError, ValueError):
            log.warning("could not restore the device settings after the scan", exc_info=True)
            self._phase = "done"

    def _advance_restore(self) -> None:
        original = self._original
        config = self._link.config
        assert original is not None
        if not self._link.is_open:
            self._phase = "done"
            return
        fresh = config is not None and config is not self._restore_from
        if fresh and config is not None and config.sweep_points == original.sweep_points:
            if self._phase == "restore_points":
                try:
                    self._link.set_span(original.start_hz, original.stop_hz)
                except (RuntimeError, ValueError):
                    log.warning("could not restore the span after the scan", exc_info=True)
                    self._phase = "done"
                    return
                self._phase = "restore_span"
                self._restore_from = config
                self._last_progress = self._clock()
                return
            tol = max(original.step_hz, _KHZ)
            if (
                abs(config.start_hz - original.start_hz) <= tol
                and abs(config.stop_hz - original.stop_hz) <= tol
            ):
                self._phase = "done"
                return
        if self._clock() - self._last_progress > self._settle_timeout:
            log.warning("the device did not confirm its restored settings; giving up")
            self._phase = "done"

    def _progress(self) -> ScanProgress:
        count = len(self._segments)
        done = self._phase == "done"
        if self._result is not None:
            fraction = 1.0
        elif self._phase in ("idle", "scanning"):
            fraction = (self._index + len(self._collected) / self._sweeps_per_segment) / count
        else:
            fraction = self._index / count
        return ScanProgress(self._index, count, fraction, self._partial, done, self._stalled)


__all__ = [
    "MAX_RETUNES",
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
