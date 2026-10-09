"""Noise floor, detected carriers and channel occupancy of one trace (pure)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from opencoord.core.occupancy import ChannelLike, ChannelOccupancy, channel_occupancy
from opencoord.core.traces import detected_carriers, noise_floor
from opencoord.core.types import Carrier, Trace

#: Carrier threshold above the noise floor (dB) when no threshold line is set.
DEFAULT_CARRIER_THRESHOLD_DB: Final = 10.0
#: The carrier list keeps the strongest this many carriers.
MAX_CARRIER_ROWS: Final = 32


class PlanLike(Protocol):
    """The part of a channel plan the analysis needs (``coord.channel_plans.ChannelPlan``)."""

    @property
    def channels(self) -> Sequence[ChannelLike]: ...

    def channel_at(self, freq_hz: float) -> ChannelLike | None: ...


@dataclass(frozen=True)
class CarrierRow:
    """A detected carrier and the plan channel it falls in (``None`` outside the plan)."""

    carrier: Carrier
    channel: int | None


@dataclass(frozen=True, eq=False)
class Analysis:
    """Analysis of a trace. ``carriers`` are sorted by frequency (the strongest
    ``MAX_CARRIER_ROWS`` when there are more); ``occupancy`` is empty without a plan.
    Compared by identity: a new analysis is a new object."""

    trace_label: str
    floor_dbm: float
    #: dB above the floor a carrier / occupied bin must reach.
    threshold_db: float
    #: ``threshold_db`` comes from the threshold line (else the default floor + 10 dB).
    from_threshold_line: bool
    carriers: tuple[CarrierRow, ...]
    occupancy: tuple[ChannelOccupancy, ...]


def analyze(
    trace: Trace,
    plan: PlanLike | None,
    threshold_dbm: float | None,
    max_rows: int | None = MAX_CARRIER_ROWS,
) -> Analysis | None:
    """Analyse ``trace``; ``None`` for an empty trace.
    ``max_rows`` caps the carriers (``None`` = all).

    The threshold is ``threshold_dbm`` when given, else the noise floor plus 10 dB.
    """
    if len(trace.dbm) == 0:
        return None
    floor = noise_floor(trace.dbm)
    threshold_db = DEFAULT_CARRIER_THRESHOLD_DB if threshold_dbm is None else threshold_dbm - floor
    carriers = detected_carriers(trace, floor, threshold_db)
    if max_rows is not None and len(carriers) > max_rows:
        carriers = sorted(carriers, key=lambda c: c.level_dbm, reverse=True)[:max_rows]
        carriers.sort(key=lambda c: c.freq_hz)
    rows = []
    for carrier in carriers:
        channel = plan.channel_at(carrier.freq_hz) if plan else None
        rows.append(CarrierRow(carrier, channel.number if channel else None))
    occupancy = channel_occupancy(trace, plan.channels, floor, threshold_db) if plan else []
    return Analysis(
        trace.label,
        floor,
        threshold_db,
        threshold_dbm is not None,
        tuple(rows),
        tuple(occupancy),
    )
