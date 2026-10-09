"""analyze(): floor, carriers and occupancy of one trace (pure)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from opencoord.core.analysis import MAX_CARRIER_ROWS, analyze
from opencoord.core.types import Trace

MHZ = 1_000_000


@dataclass(frozen=True)
class Ch:
    number: int
    start_hz: int
    stop_hz: int


@dataclass(frozen=True)
class Plan:
    channels: tuple[Ch, ...]

    def channel_at(self, freq_hz: float) -> Ch | None:
        return next((c for c in self.channels if c.start_hz <= freq_hz < c.stop_hz), None)


def _trace(peaks: dict[float, float], n: int = 400, start_mhz: float = 470.0) -> Trace:
    freqs = start_mhz * 1e6 + 100_000.0 * np.arange(n, dtype=np.float64)
    dbm = np.full(n, -100.0, dtype=np.float32)
    for mhz, level in peaks.items():
        dbm[round((mhz - start_mhz) * 10)] = level
    return Trace(freqs, dbm, "T")


PLAN = Plan((Ch(21, 470 * MHZ, 478 * MHZ), Ch(22, 478 * MHZ, 486 * MHZ)))


def test_default_threshold_is_floor_plus_ten() -> None:
    a = analyze(_trace({475.0: -50.0, 480.5: -95.0}), PLAN, None)
    assert a is not None
    assert (a.floor_dbm, a.threshold_db, a.from_threshold_line) == (-100.0, 10.0, False)
    assert [(r.carrier.freq_hz, r.channel) for r in a.carriers] == [(475 * MHZ, 21)]


def test_threshold_line_sets_the_threshold() -> None:
    a = analyze(_trace({475.0: -50.0, 480.5: -95.0}), PLAN, -97.0)
    assert a is not None
    assert a.threshold_db == pytest.approx(3.0) and a.from_threshold_line
    assert len(a.carriers) == 2
    assert a.carriers[1].channel == 22


def test_no_plan_means_no_channels_and_no_occupancy() -> None:
    a = analyze(_trace({475.0: -50.0}), None, None)
    assert a is not None and a.occupancy == () and a.carriers[0].channel is None


def test_empty_trace_gives_none() -> None:
    empty = Trace(np.array([], dtype=np.float64), np.array([], dtype=np.float32), "e")
    assert analyze(empty, PLAN, None) is None


def test_only_the_strongest_carriers_are_kept_sorted_by_frequency() -> None:
    peaks = {470.0 + 1.0 * i: -60.0 + i * 0.5 for i in range(2, MAX_CARRIER_ROWS + 8)}
    a = analyze(_trace(peaks), None, None)
    assert a is not None and len(a.carriers) == MAX_CARRIER_ROWS
    freqs = [r.carrier.freq_hz for r in a.carriers]
    assert freqs == sorted(freqs)
    weakest_kept = min(r.carrier.level_dbm for r in a.carriers)
    assert weakest_kept == pytest.approx(sorted(peaks.values())[-MAX_CARRIER_ROWS])
