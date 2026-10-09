"""Scan panel: mode, range (start/stop and center/span in MHz), presets, resolution, progress."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.core.settings import WATERFALL_DEPTH_MAX, WATERFALL_DEPTH_MIN
from opencoord.device.scanner import PRESETS, Resolution
from opencoord.ui import shortcuts, theme
from opencoord.ui.controller import Controller
from opencoord.ui.state import AppState, Mode

#: Shared values of the combos that also sit in the toolbar. (The mode radios are set by tag:
#: a radio button does not redraw its selection when its ``source`` value changes.)
MODE_RADIOS = ("scan.mode", "toolbar.mode")
PRESET_VALUE = "ui.preset"
RESOLUTION_VALUE = "ui.resolution"
CUSTOM = "Custom range"
MODES: dict[str, Mode] = {"Live": "live", "Scan": "scan"}
TAG_RUN = "scan.run"
_RANGE_INPUTS = ("scan.start", "scan.stop", "scan.center", "scan.span")
TAG_DEPTH = "scan.depth"


def resolution_labels() -> dict[str, Resolution]:
    """Combo labels (with the bin width) mapped to the resolution."""
    out: dict[str, Resolution] = {}
    for res, preset in PRESETS.items():
        bin_khz = preset.segment_span_hz / (preset.sweep_points - 1) / 1e3
        out[f"{res.value.capitalize()} ({bin_khz:.0f} kHz bins)"] = res
    return out


def mode_label(mode: Mode) -> str:
    return next(label for label, m in MODES.items() if m == mode)


def run_label(state: AppState) -> str:
    if state.stopping:
        return "Stopping..."
    if state.running:
        return "Stop"
    return "Start live" if state.mode == "live" else "Start scan"


def estimate_text(state: AppState) -> str:
    if state.mode == "live":
        return "Live: the device sweeps the range (clamped to its max span)"
    if state.scan_estimate_s is None:
        return "Estimated scan time: connect a device"
    s = state.scan_estimate_s
    return (
        f"Estimated scan time: {s / 60:.1f} min" if s >= 90 else f"Estimated scan time: {s:.0f} s"
    )


def progress_overlay(state: AppState) -> tuple[float, str]:
    p = state.scan_progress
    if p is None:
        return 0.0, ""
    if state.stopping:
        return p.fraction, "Restoring the device..."
    return p.fraction, f"Segment {min(p.segment_index + 1, p.segment_count)} / {p.segment_count}"


class ScanPanel:
    def __init__(self, controller: Controller) -> None:
        self._c = controller
        self._version = -1
        self._progress: object = None
        self._preset_labels: list[str] = []

    @property
    def text_inputs(self) -> list[str]:
        return [*_RANGE_INPUTS, TAG_DEPTH]

    # --- callbacks shared with the toolbar ---

    def on_mode(self, _sender: object, label: str) -> None:
        self._c.set_mode(MODES[label])

    def on_preset(self, _sender: object, label: str) -> None:
        if label != CUSTOM:
            self._c.set_preset(label)

    def on_resolution(self, _sender: object, label: str) -> None:
        self._c.set_resolution(resolution_labels()[label])

    def _on_range(self, *_: object) -> None:
        self._c.set_range(_hz(dpg.get_value("scan.start")), _hz(dpg.get_value("scan.stop")))

    def _on_center_span(self, *_: object) -> None:
        self._c.set_center_span(_hz(dpg.get_value("scan.center")), _hz(dpg.get_value("scan.span")))

    def build(self) -> None:
        c = self._c
        dpg.add_text("Mode")
        dpg.add_radio_button(list(MODES), tag="scan.mode", horizontal=True, callback=self.on_mode)
        dpg.add_text("Preset")
        dpg.add_combo(tag="scan.preset", source=PRESET_VALUE, width=-1, callback=self.on_preset)
        dpg.add_text("Range (MHz)")
        fmt = {"format": "%.3f", "on_enter": True, "step": 0, "width": 110}
        with dpg.table(header_row=False, policy=dpg.mvTable_SizingFixedFit):
            for _ in range(4):
                dpg.add_table_column()
            with dpg.table_row():
                dpg.add_text("Start")
                dpg.add_input_double(tag="scan.start", callback=self._on_range, **fmt)
                dpg.add_text("Stop")
                dpg.add_input_double(tag="scan.stop", callback=self._on_range, **fmt)
            with dpg.table_row():
                dpg.add_text("Center")
                dpg.add_input_double(tag="scan.center", callback=self._on_center_span, **fmt)
                dpg.add_text("Span")
                dpg.add_input_double(tag="scan.span", callback=self._on_center_span, **fmt)
        dpg.add_text("Press Enter to apply a typed value.", color=theme.MUTED_COLOR)
        dpg.add_text("Scan resolution")
        dpg.add_combo(
            list(resolution_labels()),
            tag="scan.resolution",
            source=RESOLUTION_VALUE,
            width=-1,
            callback=self.on_resolution,
        )
        dpg.add_text("", tag="scan.estimate", wrap=330)
        dpg.add_progress_bar(tag="scan.progress", width=-1)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Start", tag=TAG_RUN, width=120, callback=lambda: c.toggle())
            dpg.add_button(label="Reset max hold", callback=lambda: c.reset_max_hold())
        dpg.add_separator()
        dpg.add_input_int(
            tag=TAG_DEPTH,
            label="Waterfall rows",
            default_value=c.state.waterfall.depth,
            min_value=WATERFALL_DEPTH_MIN,
            max_value=WATERFALL_DEPTH_MAX,
            min_clamped=True,
            max_clamped=True,
            on_enter=True,
            step=0,
            width=150,
            callback=lambda _s, value: c.set_waterfall_depth(int(value)),
        )
        dpg.add_separator()
        dpg.add_text(shortcuts.help_text(), wrap=330)

    def update(self, state: AppState) -> None:
        if state.scan_progress is not self._progress or state.stopping:
            fraction, overlay = progress_overlay(state)
            dpg.configure_item("scan.progress", overlay=overlay)
            dpg.set_value("scan.progress", fraction)
            self._progress = state.scan_progress
        if state.ui_version == self._version:
            return
        self._version = state.ui_version
        labels = [p.name for p in self._c.available_presets()]
        if state.preset is None:
            labels.append(CUSTOM)
        if labels != self._preset_labels:
            self._preset_labels = labels
            for tag in ("scan.preset", "toolbar.preset"):
                if dpg.does_item_exist(tag):
                    dpg.configure_item(tag, items=labels)
        dpg.set_value(PRESET_VALUE, state.preset or CUSTOM)
        for tag in MODE_RADIOS:
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, mode_label(state.mode))
        res = next(lbl for lbl, r in resolution_labels().items() if r == state.resolution)
        dpg.set_value(RESOLUTION_VALUE, res)
        values = {
            "scan.start": state.start_hz / 1e6,
            "scan.stop": state.stop_hz / 1e6,
            "scan.center": state.center_hz / 1e6,
            "scan.span": state.span_hz / 1e6,
        }
        for tag, value in values.items():
            if not dpg.is_item_active(tag):  # never overwrite what the user is typing
                dpg.set_value(tag, value)
        if not dpg.is_item_active(TAG_DEPTH):
            dpg.set_value(TAG_DEPTH, state.waterfall.depth)
        dpg.set_value("scan.estimate", estimate_text(state))
        connected = state.connection == "connected"
        for tag in (TAG_RUN, "toolbar.run"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(
                    tag, label=run_label(state), enabled=connected and not state.stopping
                )


def _hz(mhz: float) -> int:
    return round(float(mhz) * 1e6)


__all__ = [
    "CUSTOM",
    "MODES",
    "ScanPanel",
    "estimate_text",
    "mode_label",
    "progress_overlay",
    "resolution_labels",
    "run_label",
]
