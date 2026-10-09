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

from opencoord.coord.channel_plans import ChannelPlan
from opencoord.core.markers import Marker
from opencoord.core.traces import TraceSet
from opencoord.core.types import DeviceConfig, ExclusionZone, ModelInfo, Trace
from opencoord.device.link import SerialPort
from opencoord.device.models import Capabilities
from opencoord.device.scanner import Resolution, ScanProgress

#: Columns of the waterfall (both live and scan traces are resampled to this width).
DISPLAY_BINS = 1024
DEFAULT_WATERFALL_DEPTH = 300

Mode = Literal["live", "scan"]
Connection = Literal["disconnected", "connecting", "connected", "reconnecting", "disconnecting"]


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
    A degenerate range (``hi_hz <= lo_hz``, e.g. a one-point trace) fills every column with the
    maximum level (NaN when there are no points).
    """
    if hi_hz <= lo_hz:
        level = float(np.max(dbm)) if len(dbm) else np.nan
        return np.full(bins, level, dtype=np.float32)
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
    ``version`` changes on every change; ``pushes`` counts rows pushed and ``generation`` changes
    on ``clear``/``set_depth``, so a view can colour-map only the rows added since it last looked.
    """

    def __init__(self, depth: int = DEFAULT_WATERFALL_DEPTH, bins: int = DISPLAY_BINS) -> None:
        if depth < 1 or bins < 1:
            raise ValueError("depth and bins must be >= 1")
        self.bins = bins
        self.rows: npt.NDArray[np.float32] = np.full((depth, bins), np.nan, dtype=np.float32)
        self.count = 0
        self.range_hz: tuple[int, int] | None = None
        self.version = 0
        self.pushes = 0
        self.generation = 0

    @property
    def depth(self) -> int:
        return int(self.rows.shape[0])

    def clear(self) -> None:
        self.rows[:] = np.nan
        self.count = 0
        self.range_hz = None
        self.version += 1
        self.generation += 1

    def push(self, trace: Trace) -> None:
        rng = (trace.start_hz, trace.stop_hz)
        if rng != self.range_hz:
            self.clear()
            self.range_hz = rng
        self.rows[1:] = self.rows[:-1]
        self.rows[0] = resample_max(trace.freqs_hz, trace.dbm, rng[0], rng[1], self.bins)
        self.count = min(self.count + 1, self.depth)
        self.version += 1
        self.pushes += 1

    def set_depth(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        rows = np.full((depth, self.bins), np.nan, dtype=np.float32)
        keep = min(depth, self.count)
        rows[:keep] = self.rows[:keep]
        self.rows = rows
        self.count = keep
        self.version += 1
        self.generation += 1


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
    #: The link can be tuned (``False`` for a replay: no scan mode, no span changes).
    retunable: bool = True
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
    #: Markers (at most ``MAX_MARKERS``), the selected one, and the delta reference marker id.
    markers: list[Marker] = field(default_factory=list)
    selected_marker: int | None = None
    delta_reference: int | None = None
    #: Threshold line level in dBm (``None`` = hidden).
    threshold_dbm: float | None = None
    #: Frozen reference traces by key (``ref1`` .. ``ref4``), in the order they were frozen.
    references: dict[str, Trace] = field(default_factory=dict)
    #: Keys of traces the user hid (any of ``trace_map`` keys).
    hidden_traces: set[str] = field(default_factory=set)
    #: Y limits requested by auto-scale; ``y_limits_version`` changes on every request.
    y_limits: tuple[float, float] | None = None
    y_limits_version: int = 0
    #: Cursor frequency while the mouse is over the spectrum plot, else ``None`` (set by the view).
    cursor_hz: int | None = None
    #: Channel overlay on the spectrum: on/off and the plan (``None`` if it could not be loaded).
    overlay_enabled: bool = False
    channel_plan: ChannelPlan | None = None
    #: User exclusion zones (at most ``MAX_EXCLUSION_ZONES``), ordered by id.
    exclusion_zones: list[ExclusionZone] = field(default_factory=list)
    #: Amplitude offsets in dB by device key (see ``Controller.amp_offset_key``); 0 is not stored.
    amp_offsets: dict[str, float] = field(default_factory=dict)
    #: Latest long-run logger alert (``None`` = none / cleared), shown in the status bar.
    logger_alert: str | None = None
    ui_version: int = 0
    trace_version: int = 0

    def trace_map(self) -> dict[str, Trace | None]:
        """Every plottable trace by key: live, avg, min, max, scan (partial) and the references."""
        t = self.traces
        out: dict[str, Trace | None] = {
            "live": t.live,
            "avg": t.average,
            "min": t.min_hold,
            "max": t.max_hold,
            "scan": self.scan_partial,
        }
        out.update(self.references)
        return out

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
