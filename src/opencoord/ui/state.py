"""UI state that does not depend on Dear PyGui: what the views render.

``AppState`` is owned and mutated by :class:`opencoord.ui.controller.Controller`; the Dear PyGui
modules only read it. The ``*_version`` counters let a view skip work when nothing changed:
``ui_version`` covers connection, mode, range and other widget values, ``trace_version`` the
plotted traces, and ``WaterfallHistory.version`` the waterfall rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from opencoord.core.traces import TraceSet
from opencoord.core.types import DeviceConfig, ModelInfo, Trace
from opencoord.device.link import SerialPort
from opencoord.device.models import Capabilities
from opencoord.device.scanner import Resolution, ScanProgress

#: Columns of the waterfall (both live and scan traces are resampled to this width).
DISPLAY_BINS = 1024
DEFAULT_WATERFALL_DEPTH = 300

Mode = Literal["live", "scan"]
Connection = Literal["disconnected", "connecting", "connected", "reconnecting"]


def resample_max(
    freqs_hz: npt.NDArray[np.float64],
    dbm: npt.NDArray[np.float32],
    lo_hz: float,
    hi_hz: float,
    bins: int,
) -> npt.NDArray[np.float32]:
    """``dbm`` on ``bins`` equal columns over ``lo_hz..hi_hz``.

    A column holding points takes their maximum (so a one-bin carrier survives downsampling);
    an empty column inside the data is linearly interpolated, and one outside it is NaN.
    """
    out = np.full(bins, -np.inf, dtype=np.float32)
    width = (hi_hz - lo_hz) / bins
    col = np.floor((freqs_hz - lo_hz) / width).astype(np.int64)
    # The last point sits exactly on hi_hz; count it in the last column.
    col[freqs_hz == hi_hz] = bins - 1
    inside = (col >= 0) & (col < bins)
    np.maximum.at(out, col[inside], dbm[inside])
    empty = np.isneginf(out)
    if empty.any():
        centres = lo_hz + (np.flatnonzero(empty) + 0.5) * width
        out[empty] = np.interp(centres, freqs_hz, dbm, left=np.nan, right=np.nan)
    return out


class WaterfallHistory:
    """The last ``depth`` traces as rows of ``bins`` dBm values, newest first (row 0).

    Empty rows are NaN. A trace over a different range than the rows held clears the history.
    """

    def __init__(self, depth: int = DEFAULT_WATERFALL_DEPTH, bins: int = DISPLAY_BINS) -> None:
        if depth < 1 or bins < 1:
            raise ValueError("depth and bins must be >= 1")
        self.bins = bins
        self.rows: npt.NDArray[np.float32] = np.full((depth, bins), np.nan, dtype=np.float32)
        self.count = 0
        self.range_hz: tuple[int, int] | None = None
        self.version = 0

    @property
    def depth(self) -> int:
        return int(self.rows.shape[0])

    def clear(self) -> None:
        self.rows[:] = np.nan
        self.count = 0
        self.range_hz = None
        self.version += 1

    def push(self, trace: Trace) -> None:
        rng = (trace.start_hz, trace.stop_hz)
        if rng != self.range_hz:
            self.clear()
            self.range_hz = rng
        self.rows[1:] = self.rows[:-1]
        self.rows[0] = resample_max(trace.freqs_hz, trace.dbm, rng[0], rng[1], self.bins)
        self.count = min(self.count + 1, self.depth)
        self.version += 1

    def set_depth(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        rows = np.full((depth, self.bins), np.nan, dtype=np.float32)
        keep = min(depth, self.count)
        rows[:keep] = self.rows[:keep]
        self.rows = rows
        self.count = keep
        self.version += 1


@dataclass
class AppState:
    """Everything the views show; mutated only by the controller (on the UI thread)."""

    traces: TraceSet = field(default_factory=TraceSet)
    waterfall: WaterfallHistory = field(default_factory=WaterfallHistory)
    simulator: bool = False
    connection: Connection = "disconnected"
    #: Port being connected / connected to; ``None`` = auto-detect (or the simulator).
    port: str | None = None
    ports: list[SerialPort] = field(default_factory=list)
    model: ModelInfo | None = None
    capabilities: Capabilities | None = None
    config: DeviceConfig | None = None
    auto_connect: bool = False
    #: Last device error (shown in the device panel until the next connect).
    error: str | None = None
    #: One-line status message for the status bar.
    message: str = "Not connected"
    mode: Mode = "live"
    running: bool = False
    #: A scan is being cancelled (the device is being restored).
    stopping: bool = False
    start_hz: int = 470_000_000
    stop_hz: int = 960_000_000
    preset: str | None = None
    resolution: Resolution = Resolution.NORMAL
    scan_progress: ScanProgress | None = None
    #: Stitched trace of the scan in progress (``None`` when no scan is running).
    scan_partial: Trace | None = None
    scan_estimate_s: float | None = None
    #: Range the plots show (the confirmed live span, or the scan range).
    view_range_hz: tuple[int, int] = (470_000_000, 960_000_000)
    sweeps_per_s: float = 0.0
    ui_version: int = 0
    trace_version: int = 0

    @property
    def center_hz(self) -> int:
        return (self.start_hz + self.stop_hz) // 2

    @property
    def span_hz(self) -> int:
        return self.stop_hz - self.start_hz

    @property
    def device_range_hz(self) -> tuple[int, int] | None:
        caps = self.capabilities
        return None if caps is None else (caps.min_hz, caps.max_hz)

    @property
    def busy(self) -> bool:
        """Acquiring, or a scan is still being stopped."""
        return self.running or self.stopping


__all__ = [
    "DEFAULT_WATERFALL_DEPTH",
    "DISPLAY_BINS",
    "AppState",
    "Connection",
    "Mode",
    "WaterfallHistory",
    "resample_max",
]
