"""Spectrum plot: live / max / average / min traces, the scan in progress, hover readout.

Series data is pushed with ``dpg.set_value`` only when ``state.trace_version`` changed; numpy
arrays are converted to lists once per new data. The x axis is MHz, the y axis dBm.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import numpy as np

from opencoord.core.types import Trace
from opencoord.ui import theme
from opencoord.ui.state import AppState

#: (series key, legend label); tags are ``spectrum.trace.<key>``.
SERIES: tuple[tuple[str, str], ...] = (
    ("live", "Live"),
    ("avg", "Average"),
    ("min", "Min hold"),
    ("max", "Max hold"),
    ("scan", "Scan"),
)
LEVEL_MIN_DBM = -120.0
LEVEL_MAX_DBM = -20.0

TAG_PLOT = "spectrum.plot"
TAG_X = "spectrum.x"
TAG_Y = "spectrum.y"
TAG_READOUT = "spectrum.readout"
WATERFALL_X = "waterfall.x"  # linked x axis of the waterfall plot (ui/waterfall.py)
_RELEASE_AFTER_FRAMES = 3


def series_tag(key: str) -> str:
    return f"spectrum.trace.{key}"


def traces_of(state: AppState) -> dict[str, Trace | None]:
    t = state.traces
    return {
        "live": t.live,
        "avg": t.average,
        "min": t.min_hold,
        "max": t.max_hold,
        "scan": state.scan_partial,
    }


def readout(x_mhz: float, y_dbm: float, trace: Trace | None) -> str:
    """Cursor readout, plus the level of ``trace`` at the nearest point when it covers x."""
    text = f"{x_mhz:.3f} MHz   {y_dbm:.1f} dBm"
    if trace is None or len(trace.freqs_hz) == 0:
        return text
    x_hz = x_mhz * 1e6
    if not trace.freqs_hz[0] <= x_hz <= trace.freqs_hz[-1]:
        return text
    i = int(np.clip(np.searchsorted(trace.freqs_hz, x_hz), 1, len(trace.freqs_hz) - 1))
    if abs(trace.freqs_hz[i - 1] - x_hz) <= abs(trace.freqs_hz[i] - x_hz):
        i -= 1
    return (
        f"{text}   |   {trace.label}: {float(trace.dbm[i]):.1f} dBm "
        f"at {trace.freqs_hz[i] / 1e6:.3f} MHz"
    )


class SpectrumView:
    def __init__(self) -> None:
        self._trace_version = -1
        self._view_range: tuple[int, int] | None = None
        #: Frames until locked axis limits are released (they must be rendered once first).
        self._release_x = 0
        self._release_y = 0

    def build(self) -> None:
        """Add the plot (call inside the spectrum/waterfall subplots)."""
        with dpg.plot(tag=TAG_PLOT, crosshairs=True, no_title=True):
            dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast)
            dpg.add_plot_axis(dpg.mvXAxis, tag=TAG_X, no_label=True)
            with dpg.plot_axis(dpg.mvYAxis, tag=TAG_Y, label="Level (dBm)"):
                for key, label in SERIES:
                    dpg.add_line_series([], [], label=label, tag=series_tag(key))
                    dpg.bind_item_theme(series_tag(key), theme.series_theme(key))
        dpg.set_axis_limits(TAG_Y, LEVEL_MIN_DBM, LEVEL_MAX_DBM)
        self._release_y = _RELEASE_AFTER_FRAMES

    def update(self, state: AppState) -> None:
        # Limits set with set_axis_limits are locked; release them once they were rendered so the
        # user can zoom and pan, while a new range still moves the view.
        if self._release_x:
            self._release_x -= 1
            if not self._release_x:
                dpg.set_axis_limits_auto(TAG_X)
                dpg.set_axis_limits_auto(WATERFALL_X)
        if self._release_y:
            self._release_y -= 1
            if not self._release_y:
                dpg.set_axis_limits_auto(TAG_Y)
        if state.view_range_hz != self._view_range:
            lo, hi = state.view_range_hz
            for axis in (TAG_X, WATERFALL_X):
                dpg.set_axis_limits(axis, lo / 1e6, hi / 1e6)
            self._view_range = state.view_range_hz
            self._release_x = _RELEASE_AFTER_FRAMES
        if state.trace_version != self._trace_version:
            for key, trace in traces_of(state).items():
                if trace is None:
                    dpg.set_value(series_tag(key), [[], []])
                else:
                    dpg.set_value(
                        series_tag(key), [(trace.freqs_hz / 1e6).tolist(), trace.dbm.tolist()]
                    )
            self._trace_version = state.trace_version
        if dpg.is_item_hovered(TAG_PLOT):
            x, y = dpg.get_plot_mouse_pos()
            t = traces_of(state)
            ref = t["max"] or t["scan"] or t["live"]
            dpg.set_value(TAG_READOUT, readout(x, y, ref))


__all__ = ["SERIES", "SpectrumView", "readout", "series_tag", "traces_of"]
