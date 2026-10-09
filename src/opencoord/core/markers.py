"""Marker math (pure): level lookup, peak search and delta between markers.

A :class:`Marker` is a frequency attached to a named trace (``"max"``, ``"live"``, ``"ref1"`` ...);
the functions here read levels from :class:`~opencoord.core.types.Trace` objects and know nothing
about where the traces come from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np

from opencoord.core.traces import find_peaks
from opencoord.core.types import Trace

#: The most markers shown at once.
MAX_MARKERS = 8
#: Default prominence (dB) a bump needs to count as a peak for :func:`next_peak`.
DEFAULT_MIN_PROMINENCE_DB = 3.0

Direction = Literal["left", "right"]


@dataclass(frozen=True)
class Marker:
    """Marker ``M<id>`` (id 1-based) at ``freq_hz``, reading the trace named ``trace_key``."""

    id: int
    freq_hz: int
    trace_key: str


def _nearest_index(trace: Trace, freq_hz: float) -> int:
    freqs = trace.freqs_hz
    i = int(np.clip(np.searchsorted(freqs, freq_hz), 1, max(len(freqs) - 1, 1)))
    if i >= len(freqs) or abs(freqs[i - 1] - freq_hz) <= abs(freqs[i] - freq_hz):
        i -= 1
    return i


def level_at(trace: Trace, freq_hz: float) -> float | None:
    """Level (dBm) of the bin nearest to ``freq_hz``; ``None`` when outside the trace's range."""
    if len(trace.freqs_hz) == 0 or not trace.freqs_hz[0] <= freq_hz <= trace.freqs_hz[-1]:
        return None
    return float(trace.dbm[_nearest_index(trace, freq_hz)])


def peak(trace: Trace) -> int:
    """Frequency (Hz) of the highest bin (the first one on a tie)."""
    if len(trace.dbm) == 0:
        raise ValueError("cannot find the peak of an empty trace")
    return round(float(trace.freqs_hz[int(np.argmax(trace.dbm))]))


def next_peak(
    trace: Trace,
    from_freq: float,
    direction: Direction,
    min_prominence_db: float = DEFAULT_MIN_PROMINENCE_DB,
) -> int | None:
    """Frequency (Hz) of the nearest peak strictly right/left of ``from_freq``, or ``None``.

    Peaks are those of :func:`~opencoord.core.traces.find_peaks` with ``min_prominence_db``.
    """
    idx = find_peaks(trace.freqs_hz, trace.dbm, min_prominence_db, 0.0)
    freqs = sorted(round(float(trace.freqs_hz[i])) for i in idx)
    if direction == "right":
        return next((f for f in freqs if f > from_freq), None)
    return next((f for f in reversed(freqs) if f < from_freq), None)


def delta(a: Marker, b: Marker, traces: Mapping[str, Trace]) -> tuple[int, float]:
    """``(a - b)`` as ``(frequency difference in Hz, level difference in dB)``.

    Each marker is read from ``traces[marker.trace_key]``. Raises ``KeyError`` for a missing trace
    and ``ValueError`` when a marker lies outside its trace.
    """
    la = level_at(traces[a.trace_key], a.freq_hz)
    lb = level_at(traces[b.trace_key], b.freq_hz)
    if la is None or lb is None:
        raise ValueError("a marker lies outside its trace")
    return a.freq_hz - b.freq_hz, la - lb


__all__ = [
    "DEFAULT_MIN_PROMINENCE_DB",
    "MAX_MARKERS",
    "Direction",
    "Marker",
    "delta",
    "level_at",
    "next_peak",
    "peak",
]
