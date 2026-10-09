"""Trace accumulation and spectrum analysis (pure: numpy only, no I/O).

* :class:`TraceSet` keeps live / max-hold / average / min-hold traces fed from sweeps.
* :func:`noise_floor`, :func:`find_peaks` and :func:`detected_carriers` analyse a trace.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
from collections.abc import Iterable

import numpy as np
import numpy.typing as npt

from opencoord.core.types import Carrier, Sweep, Trace

#: Percentile of all bins used as the noise-floor estimate (see :func:`noise_floor`).
NOISE_FLOOR_PERCENTILE = 20.0
#: Minimum prominence (dB) for a peak to count as a carrier in :func:`detected_carriers`.
CARRIER_MIN_PROMINENCE_DB = 3.0


def _frozen(a: npt.NDArray[np.generic]) -> None:
    a.setflags(write=False)


def _axes_equal(a: npt.NDArray[np.float64], b: npt.NDArray[np.float64]) -> bool:
    return len(a) == len(b) and a[0] == b[0] and a[-1] == b[-1]


class TraceSet:
    """Live, max-hold, average and min-hold traces built from successive sweeps.

    * ``average`` is the exact arithmetic mean, in the dB domain, of the last ``average_count``
      sweeps (a sliding window kept in a deque with a running float64 sum), so it always lies
      between min-hold and max-hold. Before ``average_count`` sweeps have arrived it is the mean
      of those received so far.
    * Max/min hold accumulate since the last :meth:`reset`.
    * A sweep whose axis differs from the current one, or from the axis of any published trace
      (different length, start or stop), resets every trace and starts over with that sweep.
    * :meth:`restore` shows saved traces (a session): the next sweep on the same axis continues
      their max/min hold, any other sweep replaces them.

    Trace objects are never mutated after being published; each update creates new arrays, and
    the published arrays are read-only. Sweeps containing non-finite levels (NaN/inf) are rejected
    with ``ValueError`` because they would permanently poison the running average.
    """

    def __init__(self, average_count: int = 10) -> None:
        self._average_count = self._validate_count(average_count)
        self._window: deque[npt.NDArray[np.float32]] = deque()
        self._sum: npt.NDArray[np.float64] | None = None
        self._freqs: npt.NDArray[np.float64] | None = None
        self.live: Trace | None = None
        self.max_hold: Trace | None = None
        self.average: Trace | None = None
        self.min_hold: Trace | None = None

    @staticmethod
    def _validate_count(n: int) -> int:
        if n < 1:
            raise ValueError("average_count must be >= 1")
        return n

    @property
    def average_count(self) -> int:
        return self._average_count

    @average_count.setter
    def average_count(self, n: int) -> None:
        self._average_count = self._validate_count(n)
        trimmed = False
        while len(self._window) > n:
            old = self._window.popleft()
            assert self._sum is not None
            self._sum -= old
            trimmed = True
        if trimmed:
            self._publish_average()

    def reset(self) -> None:
        """Forget all accumulated data (``average_count`` is kept)."""
        self._window.clear()
        self._sum = None
        self._freqs = None
        self.live = self.max_hold = self.average = self.min_hold = None

    def restore(
        self,
        *,
        live: Trace | None = None,
        max_hold: Trace | None = None,
        average: Trace | None = None,
        min_hold: Trace | None = None,
    ) -> None:
        """Replace everything with saved traces (e.g. from a session).

        The stored axis becomes that of the first restored trace, so a following sweep on the same
        axis continues the max/min hold and any other sweep resets everything. The averaging
        window starts empty (the saved average is shown until the first sweep, which then starts
        a fresh average) because the sweeps behind a saved average are not known.
        """
        self.reset()
        self.live, self.max_hold, self.average, self.min_hold = live, max_hold, average, min_hold
        first = next((t for t in (live, max_hold, average, min_hold) if t is not None), None)
        if first is not None:
            self._freqs = np.array(first.freqs_hz, dtype=np.float64)
            _frozen(self._freqs)

    def update(self, sweep: Sweep) -> None:
        """Fold a sweep into all traces."""
        if len(sweep.freqs_hz) == 0 or len(sweep.freqs_hz) != len(sweep.dbm):
            raise ValueError("sweep must have a non-empty axis matching its levels")
        if not self._same_axis(sweep.freqs_hz):
            self.reset()
        if not np.all(np.isfinite(sweep.dbm)):
            raise ValueError("sweep contains non-finite levels")
        dbm = np.array(sweep.dbm, dtype=np.float32)  # private copy
        if self._freqs is None:
            self._freqs = np.array(sweep.freqs_hz, dtype=np.float64)
            _frozen(self._freqs)
        freqs = self._freqs
        if self.max_hold is None or self.min_hold is None:
            max_dbm, min_dbm = dbm.copy(), dbm.copy()
        else:
            max_dbm = np.maximum(self.max_hold.dbm, dbm)
            min_dbm = np.minimum(self.min_hold.dbm, dbm)
        window_copy = dbm.copy()  # the average window is private; callers never see it
        for arr in (dbm, max_dbm, min_dbm, window_copy):
            _frozen(arr)
        self.live = Trace(freqs, dbm, "Live")
        self.max_hold = Trace(freqs, max_dbm, "Max hold")
        self.min_hold = Trace(freqs, min_dbm, "Min hold")
        if self._sum is None:
            self._sum = np.zeros(len(dbm), dtype=np.float64)
        self._window.append(window_copy)
        self._sum += window_copy
        while len(self._window) > self._average_count:
            self._sum -= self._window.popleft()
        self._publish_average()

    def _same_axis(self, freqs: npt.NDArray[np.float64]) -> bool:
        """``freqs`` matches the stored axis and the axis of every published trace."""
        traces = (self.live, self.max_hold, self.average, self.min_hold)
        axes = [self._freqs] + [t.freqs_hz for t in traces if t is not None]
        return all(cur is None or _axes_equal(cur, freqs) for cur in axes)

    def _publish_average(self) -> None:
        assert self._sum is not None and self._freqs is not None
        mean = (self._sum / len(self._window)).astype(np.float32)
        if self.min_hold is not None and self.max_hold is not None:
            # Guard against float64 running-sum rounding residue (e.g. after cancellation).
            mean = np.clip(mean, self.min_hold.dbm, self.max_hold.dbm)
        _frozen(mean)
        self.average = Trace(self._freqs, mean, "Average")


def noise_floor(dbm: npt.NDArray[np.float32] | npt.NDArray[np.float64]) -> float:
    """Robust noise-floor estimate in dBm: the 20th percentile of all bins.

    A median would be pulled up when strong DVB-T blocks occupy ~40 % of the band (the median
    would sit at the 83rd percentile of the noise); the 20th percentile stays inside the noise
    as long as less than ~80 % of the bins are occupied, at the cost of reading roughly 1 dB
    below the mean noise level.
    """
    if dbm.size == 0:
        raise ValueError("cannot estimate the noise floor of an empty trace")
    return float(np.percentile(dbm, NOISE_FLOOR_PERCENTILE))


def _prominence(v: list[float]) -> list[float]:
    """Topographic prominence of every sample of ``v`` (adjacent samples must differ), O(n).

    For sample ``i`` the base on each side is the minimum between ``i`` and the nearest strictly
    higher sample (or the trace edge); prominence = ``v[i]`` minus the higher of the two bases.
    A side with no samples at all (``i`` at the edge) is ignored, so edge samples can be peaks.
    """

    def side_min(vals: list[float]) -> list[float]:
        stack: list[
            tuple[float, float]
        ] = []  # (value, min of the span it covers), values decreasing
        out: list[float] = []
        for x in vals:
            mn = x
            while stack and stack[-1][0] <= x:
                mn = min(mn, stack.pop()[1])
            stack.append((x, mn))
            out.append(mn)
        return out

    left = side_min(v)
    right = side_min(v[::-1])[::-1]
    left[0] = right[-1] = -np.inf
    return [x - max(lo, hi) for x, lo, hi in zip(v, left, right, strict=True)]


def find_peaks(
    freqs_hz: npt.NDArray[np.float64],
    dbm: npt.NDArray[np.float32] | npt.NDArray[np.float64],
    min_prominence_db: float,
    min_spacing_hz: float,
) -> list[int]:
    """Indices of peaks, strongest first.

    A peak is a local maximum (a flat top counts once, at its middle sample; edge samples
    qualify) whose prominence is at least ``min_prominence_db``. Peaks are then accepted in
    order of decreasing level, dropping any closer than ``min_spacing_hz`` to an already
    accepted, stronger peak. Equal levels are ordered by ascending index. A completely flat
    trace has no peaks (a single sample is one).
    """
    n = len(dbm)
    if n == 0:
        return []
    # Collapse flat runs so that plateaus (e.g. DVB-T blocks) are single samples.
    breaks = np.flatnonzero(np.diff(dbm) != 0)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [n - 1]))
    run_mid = ((starts + ends) // 2).tolist()
    run_vals = [float(x) for x in dbm[starts]]
    if len(run_vals) == 1:
        return [run_mid[0]] if n == 1 else []
    prom = _prominence(run_vals)
    last = len(run_vals) - 1
    candidates = [
        run_mid[r]
        for r in range(len(run_vals))
        if (r == 0 or run_vals[r - 1] < run_vals[r])
        and (r == last or run_vals[r + 1] < run_vals[r])
        and prom[r] >= min_prominence_db
    ]
    candidates.sort(key=lambda k: (-float(dbm[k]), k))
    if min_spacing_hz <= 0:
        return candidates
    # Accept strongest first; only the two nearest accepted neighbours (by frequency) matter.
    accepted: list[int] = []
    accepted_freqs: list[float] = []  # sorted
    for k in candidates:
        f = float(freqs_hz[k])
        pos = bisect_left(accepted_freqs, f)
        if pos > 0 and f - accepted_freqs[pos - 1] < min_spacing_hz:
            continue
        if pos < len(accepted_freqs) and accepted_freqs[pos] - f < min_spacing_hz:
            continue
        insort(accepted_freqs, f)
        accepted.append(k)
    return accepted


def detected_carriers(
    trace: Trace,
    floor_dbm: float,
    threshold_db: float,
    *,
    min_prominence_db: float = CARRIER_MIN_PROMINENCE_DB,
    min_spacing_hz: float = 0.0,
) -> list[Carrier]:
    """Peaks at least ``threshold_db`` above ``floor_dbm``, sorted by ascending frequency.

    Peaks must also have ``min_prominence_db`` prominence so ripple on a wide block is not
    reported as separate carriers.
    """
    peaks = find_peaks(trace.freqs_hz, trace.dbm, min_prominence_db, min_spacing_hz)
    carriers = [
        Carrier(freq_hz=round(float(trace.freqs_hz[i])), level_dbm=float(trace.dbm[i]))
        for i in peaks
        if float(trace.dbm[i]) >= floor_dbm + threshold_db
    ]
    carriers.sort(key=lambda c: c.freq_hz)
    return carriers


def auto_scale_limits(
    traces: Iterable[Trace], padding_db: float = 5.0
) -> tuple[float, float] | None:
    """``(low, high)`` dBm covering every trace plus ``padding_db``; ``None`` without any data."""
    lows = [float(np.min(t.dbm)) for t in traces if len(t.dbm)]
    highs = [float(np.max(t.dbm)) for t in traces if len(t.dbm)]
    if not lows:
        return None
    return min(lows) - padding_db, max(highs) + padding_db
