"""Spectrum plot: traces, reference traces, markers, threshold line, hover readout.

Series data is pushed with ``dpg.set_value`` only when ``state.trace_version`` changed; numpy
arrays are converted to lists once per new data. The x axis is MHz, the y axis dBm.

Markers are vertical ``drag_line`` items with an annotation, drawn from a fixed pool of
``MAX_MARKERS`` slots that are shown or hidden as markers come and go. Dragging a line calls
``Controller.move_marker``. Panning and zooming keep their default mouse bindings, so a marker is
placed with **Ctrl+click** on the plot (or the M key, see ``shortcuts.py``).
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import numpy as np

from opencoord.core.markers import MAX_MARKERS
from opencoord.core.types import Trace
from opencoord.ui import theme
from opencoord.ui.controller import MAX_REFERENCES, Controller, MarkerRow
from opencoord.ui.overlay import OverlayView
from opencoord.ui.state import AppState

#: (series key, legend label); tags are ``spectrum.trace.<key>``.
SERIES: tuple[tuple[str, str], ...] = (
    ("live", "Live"),
    ("avg", "Average"),
    ("min", "Min hold"),
    ("max", "Max hold"),
    ("scan", "Scan"),
    *((f"ref{i}", f"Ref {i}") for i in range(1, MAX_REFERENCES + 1)),
)
LEVEL_MIN_DBM = -120.0
LEVEL_MAX_DBM = -20.0

TAG_PLOT = "spectrum.plot"
TAG_X = "spectrum.x"
TAG_Y = "spectrum.y"
TAG_READOUT = "spectrum.readout"
TAG_THRESHOLD = "spectrum.threshold"
WATERFALL_X = "waterfall.x"  # linked x axis of the waterfall plot (ui/waterfall.py)
_RELEASE_AFTER_FRAMES = 3
#: A drag line is only moved from the state when it is further than this (MHz / dB) from it.
_LINE_TOLERANCE = 1e-5


def series_tag(key: str) -> str:
    return f"spectrum.trace.{key}"


def marker_line_tag(slot: int) -> str:
    return f"spectrum.marker.{slot}"


def marker_note_tag(slot: int) -> str:
    return f"spectrum.marker.{slot}.note"


def traces_of(state: AppState) -> dict[str, Trace | None]:
    return state.trace_map()


def marker_text(row: MarkerRow) -> str:
    """Annotation / table label, e.g. ``M1 612.350 MHz -67.2 dBm`` (ASCII: the default font)."""
    text = f"M{row.marker.id} {row.marker.freq_hz / 1e6:.3f} MHz"
    return text if row.level_dbm is None else f"{text} {row.level_dbm:.1f} dBm"


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


def _line_value(sender: object) -> float:
    value = dpg.get_value(sender)
    return float(value[0] if isinstance(value, list | tuple) else value)


class SpectrumView:
    def __init__(self, controller: Controller) -> None:
        self._c = controller
        self._trace_version = -1
        self._ui_version = -1
        self._y_version = 0
        self._view_range: tuple[int, int] | None = None
        #: Frames until locked axis limits are released (they must be rendered once first).
        self._release_x = 0
        self._release_y = 0
        #: Marker id shown in each pool slot, and the x (MHz) each line was last set to.
        self._slot_ids: list[int | None] = [None] * MAX_MARKERS
        self._slot_x: list[float] = [np.nan] * MAX_MARKERS
        self._threshold_y = np.nan
        self.overlay = OverlayView(controller, TAG_X)

    def build(self) -> None:
        """Add the plot (call inside the spectrum/waterfall subplots)."""
        with dpg.plot(tag=TAG_PLOT, crosshairs=True, no_title=True):
            # Legend buttons are off: trace visibility is state (Markers tab) so it stays in sync.
            dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast, no_buttons=True)
            dpg.add_plot_axis(dpg.mvXAxis, tag=TAG_X, no_label=True)
            with dpg.plot_axis(dpg.mvYAxis, tag=TAG_Y, label="Level (dBm)"):
                self.overlay.build_grid()
                for key, label in SERIES:
                    dpg.add_line_series([], [], label=label, tag=series_tag(key))
                    dpg.bind_item_theme(series_tag(key), theme.series_theme(key))
            self.overlay.build_layers()
            for slot in range(MAX_MARKERS):
                dpg.add_drag_line(
                    tag=marker_line_tag(slot),
                    vertical=True,
                    show=False,
                    show_label=False,
                    color=theme.MARKER_COLOR,
                    callback=self._on_marker_dragged,
                    user_data=slot,
                )
                dpg.add_plot_annotation(
                    tag=marker_note_tag(slot), show=False, offset=(6, -6), clamped=True
                )
            dpg.add_drag_line(
                tag=TAG_THRESHOLD,
                vertical=False,
                show=False,
                show_label=False,
                color=theme.THRESHOLD_COLOR,
                callback=lambda s, *_: self._c.set_threshold_dbm(_line_value(s)),
            )
        dpg.set_axis_limits(TAG_Y, LEVEL_MIN_DBM, LEVEL_MAX_DBM)
        self._release_y = _RELEASE_AFTER_FRAMES
        with dpg.handler_registry(tag="spectrum.handlers"):
            dpg.add_mouse_click_handler(dpg.mvMouseButton_Left, callback=self._on_click)

    # --- input ---

    def _on_click(self, *_: object) -> None:
        """Ctrl+click on the plot places a marker (plain clicks and drags pan the plot)."""
        if dpg.is_key_down(dpg.mvKey_ModCtrl) and dpg.is_item_hovered(TAG_PLOT):
            self._c.add_marker(dpg.get_plot_mouse_pos()[0] * 1e6)

    def _on_marker_dragged(self, sender: object, _app_data: object, slot: int) -> None:
        marker_id = self._slot_ids[slot]
        if marker_id is not None:
            self._slot_x[slot] = _line_value(sender)
            self._c.move_marker(marker_id, self._slot_x[slot] * 1e6)
            self._c.select_marker(marker_id)

    # --- per frame ---

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
        if state.y_limits is not None and state.y_limits_version != self._y_version:
            dpg.set_axis_limits(TAG_Y, *state.y_limits)
            self._y_version = state.y_limits_version
            self._release_y = _RELEASE_AFTER_FRAMES
        changed = state.trace_version != self._trace_version
        if changed:
            for key, _label in SERIES:
                trace = state.trace_map().get(key)
                if trace is None:
                    dpg.set_value(series_tag(key), [[], []])
                else:
                    dpg.set_value(
                        series_tag(key), [(trace.freqs_hz / 1e6).tolist(), trace.dbm.tolist()]
                    )
                dpg.configure_item(series_tag(key), show=key not in state.hidden_traces)
        self.overlay.update(state)
        if changed or state.ui_version != self._ui_version:
            self._trace_version, self._ui_version = state.trace_version, state.ui_version
            self._update_markers(state)
            self._update_threshold(state)
        if dpg.is_item_hovered(TAG_PLOT):
            x, y = dpg.get_plot_mouse_pos()
            state.cursor_hz = round(x * 1e6)  # read by Controller.add_marker_at_cursor
            ref = self._c.resolve_trace("max")
            dpg.set_value(TAG_READOUT, readout(x, y, ref[1] if ref else None))
        else:
            state.cursor_hz = None

    def _update_markers(self, state: AppState) -> None:
        rows = self._c.marker_rows()
        for slot in range(MAX_MARKERS):
            line, note = marker_line_tag(slot), marker_note_tag(slot)
            if slot >= len(rows):
                self._slot_ids[slot] = None
                dpg.configure_item(line, show=False)
                dpg.configure_item(note, show=False)
                continue
            row = rows[slot]
            x = row.marker.freq_hz / 1e6
            self._slot_ids[slot] = row.marker.id
            selected = row.marker.id == state.selected_marker
            dpg.configure_item(
                line,
                show=True,
                color=theme.MARKER_SELECTED_COLOR if selected else theme.MARKER_COLOR,
                thickness=2.0 if selected else 1.0,
            )
            if not abs(self._slot_x[slot] - x) < _LINE_TOLERANCE:
                dpg.set_value(line, x)
                self._slot_x[slot] = x
            if row.level_dbm is None:
                dpg.configure_item(note, show=False)
            else:
                dpg.configure_item(note, show=True, label=marker_text(row))
                dpg.set_value(note, (x, row.level_dbm))

    def _update_threshold(self, state: AppState) -> None:
        level = state.threshold_dbm
        dpg.configure_item(TAG_THRESHOLD, show=level is not None)
        if level is not None and not abs(self._threshold_y - level) < _LINE_TOLERANCE:
            dpg.set_value(TAG_THRESHOLD, level)
            self._threshold_y = level


__all__ = [
    "SERIES",
    "SpectrumView",
    "marker_text",
    "readout",
    "series_tag",
    "traces_of",
]
