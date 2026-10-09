"""Per-channel occupancy of a trace (pure)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from opencoord.core.types import Trace


class ChannelLike(Protocol):
    """Anything with a number and ``start_hz <= f < stop_hz`` edges (e.g. a plan's ``Channel``)."""

    @property
    def number(self) -> int: ...

    @property
    def start_hz(self) -> int: ...

    @property
    def stop_hz(self) -> int: ...


@dataclass(frozen=True)
class ChannelOccupancy:
    number: int
    max_dbm: float
    #: Mean power over the channel's bins, expressed in dBm.
    avg_dbm: float
    #: Percentage (0-100) of the channel's bins at or above ``floor + threshold_db``.
    percent_above: float
    #: Covered bandwidth / channel width (0..1): below 1 when the trace ends inside the channel.
    coverage: float


#: Channels covered less than this are only "partial": views show no occupancy verdict for them.
PARTIAL_COVERAGE = 0.5


def channel_occupancy(
    trace: Trace,
    channels: Iterable[ChannelLike],
    floor_dbm: float,
    threshold_db: float,
) -> list[ChannelOccupancy]:
    """Max level, average power and percent of bins above ``floor_dbm + threshold_db`` per channel.

    A bin belongs to a channel when ``start_hz <= f < stop_hz``. Channels the trace has no bin in
    are left out; a channel the trace only touches or partly covers is reported with its
    ``coverage`` (a trace ending exactly on a channel edge puts one bin in the next channel, with
    coverage 0). The trace axis must be ascending (all traces are).
    """
    limit = floor_dbm + threshold_db
    freqs, dbm = trace.freqs_hz, trace.dbm.astype(np.float64)
    out: list[ChannelOccupancy] = []
    first_hz, last_hz = (
        float(freqs[0]) if len(freqs) else 0.0,
        float(freqs[-1]) if len(freqs) else 0.0,
    )
    for ch in channels:
        lo = int(np.searchsorted(freqs, ch.start_hz, side="left"))
        hi = int(np.searchsorted(freqs, ch.stop_hz, side="left"))
        if hi <= lo:
            continue
        seg = dbm[lo:hi]
        width = ch.stop_hz - ch.start_hz
        covered = min(ch.stop_hz, last_hz) - max(ch.start_hz, first_hz)
        coverage = min(max(covered / width, 0.0), 1.0) if width > 0 else 0.0
        out.append(
            ChannelOccupancy(
                number=ch.number,
                max_dbm=float(np.max(seg)),
                avg_dbm=float(10.0 * np.log10(np.mean(10.0 ** (seg / 10.0)))),
                percent_above=float(100.0 * np.count_nonzero(seg >= limit) / len(seg)),
                coverage=coverage,
            )
        )
    return out
