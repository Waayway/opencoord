"""Long-run logger: periodic max-hold CSV rows and threshold alerts.

:class:`LoggerEngine` is the pure decision logic (time is passed in): feed it sweeps; every
``interval_s`` it yields one :class:`LogRow` per range (the maximum level and its frequency since
the previous write), and :meth:`LoggerEngine.feed` returns an :class:`Alert` when any bin of a range
exceeds the threshold. An alert is not repeated for the same range within one interval. A range with
no data since the last write gets no row. :class:`LogWriter` appends lines to the CSV file.

CSV: ``timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz,kind``;
``kind`` is ``DATA`` for the row per range and interval and ``ALERT`` for an alert line.
:class:`LogWriter` never appends to a file whose header differs (another layout): it starts
``name-1.csv``, ``name-2.csv``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

import numpy as np
import numpy.typing as npt

CSV_HEADER: Final = "timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz,kind"
MIN_INTERVAL_S: Final = 1.0
DEFAULT_INTERVAL_S: Final = 60.0
MAX_RANGES: Final = 8
_MHZ = 1_000_000


@dataclass(frozen=True)
class LogRange:
    start_hz: int
    stop_hz: int

    def __post_init__(self) -> None:
        if self.start_hz >= self.stop_hz:
            raise ValueError("A range's start must be below its stop")


@dataclass(frozen=True)
class LogRow:
    timestamp_iso: str
    start_hz: int
    stop_hz: int
    max_dbm: float
    peak_hz: int


@dataclass(frozen=True)
class Alert:
    start_hz: int
    stop_hz: int
    level_dbm: float
    peak_hz: int
    threshold_dbm: float


class LoggerEngine:
    def __init__(
        self,
        ranges: list[LogRange],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        threshold_dbm: float | None = None,
    ) -> None:
        if not ranges:
            raise ValueError("The logger needs at least one range")
        if len(ranges) > MAX_RANGES:
            raise ValueError(f"The logger takes at most {MAX_RANGES} ranges")
        if not interval_s >= MIN_INTERVAL_S:
            raise ValueError(f"The logging interval must be at least {MIN_INTERVAL_S:.0f} s")
        self.ranges = list(ranges)
        self.interval_s = float(interval_s)
        self.threshold_dbm = threshold_dbm
        self._max: list[tuple[float, int] | None] = [None] * len(ranges)
        self._last_alert: list[float | None] = [None] * len(ranges)
        self._last_write = 0.0

    @property
    def last_write(self) -> float:
        """Time the current interval started (the last write, or :meth:`start`)."""
        return self._last_write

    def start(self, now: float) -> None:
        """Start the first interval at ``now``."""
        self._last_write = now

    def set_threshold(self, threshold_dbm: float | None) -> None:
        self.threshold_dbm = threshold_dbm

    def feed(
        self, freqs_hz: npt.NDArray[np.float64], dbm: npt.NDArray[np.float32], now: float
    ) -> list[Alert]:
        """Fold one sweep into the max hold; returns the alerts it raised."""
        alerts: list[Alert] = []
        for i, r in enumerate(self.ranges):
            inside = np.flatnonzero((freqs_hz >= r.start_hz) & (freqs_hz <= r.stop_hz))
            if len(inside) == 0:
                continue
            k = int(inside[np.argmax(dbm[inside])])
            level, peak = float(dbm[k]), round(float(freqs_hz[k]))
            held = self._max[i]
            if held is None or level > held[0]:
                self._max[i] = (level, peak)
            threshold = self.threshold_dbm
            if threshold is not None and level > threshold:
                last = self._last_alert[i]
                if last is None or now - last >= self.interval_s:
                    self._last_alert[i] = now
                    alerts.append(Alert(r.start_hz, r.stop_hz, level, peak, threshold))
        return alerts

    def due(self, now: float) -> bool:
        return now - self._last_write >= self.interval_s

    def take_rows(self, now: float, timestamp_iso: str) -> list[LogRow]:
        """The rows for the interval that just ended; restarts the max hold and the interval."""
        rows = [
            LogRow(timestamp_iso, r.start_hz, r.stop_hz, held[0], held[1])
            for r, held in zip(self.ranges, self._max, strict=True)
            if held is not None
        ]
        self._max = [None] * len(self.ranges)
        self._last_write = now
        return rows


def _fields(timestamp_iso: str, start_hz: int, stop_hz: int, level: float, peak_hz: int) -> str:
    return (
        f"{timestamp_iso},{start_hz / _MHZ:.6f},{stop_hz / _MHZ:.6f},{level:.1f},"
        f"{peak_hz / _MHZ:.6f}"
    )


def row_csv(row: LogRow) -> str:
    return _fields(row.timestamp_iso, row.start_hz, row.stop_hz, row.max_dbm, row.peak_hz) + ",DATA"


def alert_csv(timestamp_iso: str, alert: Alert) -> str:
    return _fields(timestamp_iso, alert.start_hz, alert.stop_hz, alert.level_dbm, alert.peak_hz) + (
        ",ALERT"
    )


def _first_line(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readline().rstrip("\r\n")


def usable_log_path(path: Path) -> Path:
    """``path``, or ``stem-N.suffix`` when ``path`` exists with another header (or unreadable)."""
    n = 0
    candidate = path
    while candidate.exists() and candidate.stat().st_size > 0:
        try:
            if _first_line(candidate) == CSV_HEADER:
                return candidate
        except OSError:
            pass
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


class LogWriter:
    """Appends CSV lines (flushed per line, so a crash loses nothing); writes the header once.

    ``path`` is where lines go: the requested file, or a numbered sibling when the requested one
    holds a different layout (``redirected`` is then true).
    """

    def __init__(self, path: Path) -> None:
        self.requested = path
        self.path = usable_log_path(path)
        self.redirected = self.path != path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        self._file: TextIO | None = open(  # noqa: SIM115
            self.path, "a", encoding="utf-8", newline="\n"
        )
        if new:
            self.write_line(CSV_HEADER)

    def write_line(self, line: str) -> None:
        if self._file is None:
            raise ValueError("The log file is closed")
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


__all__ = [
    "CSV_HEADER",
    "DEFAULT_INTERVAL_S",
    "MAX_RANGES",
    "MIN_INTERVAL_S",
    "Alert",
    "LogRange",
    "LogRow",
    "LogWriter",
    "LoggerEngine",
    "alert_csv",
    "row_csv",
    "usable_log_path",
]
