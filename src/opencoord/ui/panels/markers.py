"""Markers panel: marker table, threshold line, visible traces, reference traces, auto-scale."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.core.markers import MAX_MARKERS
from opencoord.ui import theme
from opencoord.ui.controller import MAX_REFERENCES, Controller, MarkerRow
from opencoord.ui.spectrum import SERIES
from opencoord.ui.state import AppState

TAG_THRESHOLD_ON = "markers.threshold_on"
TAG_THRESHOLD = "markers.threshold"
DEFAULT_THRESHOLD_DBM = -90.0
#: Primary traces with a visibility checkbox (the reference traces have their own rows).
_PRIMARY = tuple((key, label) for key, label in SERIES if not key.startswith("ref"))


def delta_text(delta: tuple[int, float] | None) -> str:
    """``+0.600 MHz +10.0 dB`` for a marker's delta versus the reference, else an empty string."""
    if delta is None:
        return ""
    df_hz, ddb = delta
    return f"{df_hz / 1e6:+.3f} MHz {ddb:+.1f} dB"


def level_text(row: MarkerRow) -> str:
    return "-" if row.level_dbm is None else f"{row.level_dbm:.1f}"


class MarkersPanel:
    def __init__(self, controller: Controller) -> None:
        self._c = controller
        self._versions = (-1, -1)
        self._ids: list[int | None] = [None] * MAX_MARKERS
        self._ref_keys: list[str | None] = [None] * MAX_REFERENCES
        self._last_threshold = DEFAULT_THRESHOLD_DBM

    @property
    def text_inputs(self) -> list[str]:
        return [TAG_THRESHOLD]

    # --- callbacks ---

    def _on_select(self, _sender: object, _value: object, slot: int) -> None:
        self._c.select_marker(self._ids[slot])

    def _act(self, slot: int, action: str) -> None:
        marker_id = self._ids[slot]
        if marker_id is None:
            return
        c = self._c
        if action == "delete":
            c.remove_marker(marker_id)
            return
        c.select_marker(marker_id)
        if action == "peak":
            c.marker_to_peak()
        elif action == "left":
            c.marker_next_peak("left")
        elif action == "right":
            c.marker_next_peak("right")
        elif action == "delta":
            c.set_delta_reference(None if c.state.delta_reference == marker_id else marker_id)

    def _on_threshold_on(self, _sender: object, on: bool) -> None:
        self._c.set_threshold_dbm(self._last_threshold if on else None)

    def _on_threshold_value(self, _sender: object, value: float) -> None:
        self._last_threshold = float(value)
        if self._c.state.threshold_dbm is not None:
            self._c.set_threshold_dbm(self._last_threshold)

    # --- layout ---

    def build(self) -> None:
        c = self._c
        dpg.add_text(
            "Ctrl+click the plot (or press M) to place a marker. Drag a marker line to move it.",
            wrap=330,
            color=theme.MUTED_COLOR,
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Add at peak", callback=lambda: c.add_marker_at_peak())
            dpg.add_button(
                label="Clear all", tag="markers.clear", callback=lambda: c.clear_markers()
            )
            dpg.add_button(
                label="Auto-scale", tag="markers.autoscale", callback=lambda: c.auto_scale()
            )
        dpg.add_text("", tag="markers.empty", color=theme.MUTED_COLOR)
        with dpg.table(
            tag="markers.table",
            header_row=True,
            borders_innerH=True,
            policy=dpg.mvTable_SizingStretchProp,
        ):
            dpg.add_table_column(label="Marker", init_width_or_weight=1.0)
            dpg.add_table_column(label="MHz", init_width_or_weight=1.2)
            dpg.add_table_column(label="dBm", init_width_or_weight=0.9)
            dpg.add_table_column(label="Delta vs ref", init_width_or_weight=2.0)
            for slot in range(MAX_MARKERS):
                with dpg.table_row(tag=f"markers.row.{slot}", show=False):
                    dpg.add_selectable(
                        label="",
                        tag=f"markers.select.{slot}",
                        callback=self._on_select,
                        user_data=slot,
                    )
                    dpg.add_text("", tag=f"markers.freq.{slot}")
                    dpg.add_text("", tag=f"markers.level.{slot}")
                    dpg.add_text("", tag=f"markers.delta.{slot}")
                with dpg.table_row(tag=f"markers.actions.{slot}", show=False):
                    self._action_button(slot, "peak", "Peak")
                    with dpg.group(horizontal=True):
                        self._action_button(slot, "left", "<")
                        self._action_button(slot, "right", ">")
                    self._action_button(slot, "delta", "Set ref")
                    self._action_button(slot, "delete", "Delete")
        dpg.add_separator()
        dpg.add_text("Threshold")
        with dpg.group(horizontal=True):
            dpg.add_checkbox(label="Show", tag=TAG_THRESHOLD_ON, callback=self._on_threshold_on)
            dpg.add_input_double(
                tag=TAG_THRESHOLD,
                default_value=DEFAULT_THRESHOLD_DBM,
                format="%.1f dBm",
                step=0,
                on_enter=True,
                width=120,
                callback=self._on_threshold_value,
            )
        dpg.add_text("Drag the red line in the plot to move it.", color=theme.MUTED_COLOR)
        dpg.add_separator()
        dpg.add_text("Visible traces")
        with dpg.group(horizontal=True):
            for key, label in _PRIMARY:
                dpg.add_checkbox(
                    label=label,
                    tag=f"markers.show.{key}",
                    default_value=True,
                    callback=lambda _s, v, u: c.set_trace_visible(u, v),
                    user_data=key,
                )
        dpg.add_separator()
        dpg.add_text("Reference traces")
        dpg.add_button(
            label="Freeze current trace",
            tag="markers.freeze",
            callback=lambda: c.freeze_reference(),
        )
        for slot in range(MAX_REFERENCES):
            with dpg.group(horizontal=True, tag=f"markers.ref.{slot}", show=False):
                dpg.add_checkbox(
                    tag=f"markers.ref_show.{slot}",
                    default_value=True,
                    callback=lambda _s, v, u: self._on_ref_visible(u, v),
                    user_data=slot,
                )
                dpg.add_button(
                    label="Remove",
                    small=True,
                    callback=lambda _s, _a, u: c.remove_reference(u),
                    user_data=slot,
                )

    def _action_button(self, slot: int, action: str, label: str) -> None:
        dpg.add_button(
            label=label,
            small=True,
            tag=f"markers.btn.{action}.{slot}",
            callback=lambda _s, _a, u: self._act(*u),
            user_data=(slot, action),
        )

    def _on_ref_visible(self, slot: int, visible: bool) -> None:
        key = self._ref_keys[slot]
        if key is not None:
            self._c.set_trace_visible(key, visible)

    # --- per frame ---

    def update(self, state: AppState) -> None:
        versions = (state.ui_version, state.trace_version)
        if versions == self._versions:
            return
        self._versions = versions
        rows = self._c.marker_rows()
        dpg.set_value("markers.empty", "" if rows else "No markers yet.")
        for slot in range(MAX_MARKERS):
            shown = slot < len(rows)
            dpg.configure_item(f"markers.row.{slot}", show=shown)
            dpg.configure_item(f"markers.actions.{slot}", show=shown)
            if not shown:
                self._ids[slot] = None
                continue
            row = rows[slot]
            self._ids[slot] = row.marker.id
            marker_id = row.marker.id
            mark = " (ref)" if marker_id == state.delta_reference else ""
            dpg.configure_item(f"markers.select.{slot}", label=f"M{marker_id}{mark}")
            dpg.set_value(f"markers.select.{slot}", marker_id == state.selected_marker)
            dpg.set_value(f"markers.freq.{slot}", f"{row.marker.freq_hz / 1e6:.3f}")
            dpg.set_value(f"markers.level.{slot}", level_text(row))
            dpg.set_value(f"markers.delta.{slot}", delta_text(row.delta))
        on = state.threshold_dbm is not None
        dpg.set_value(TAG_THRESHOLD_ON, on)
        if state.threshold_dbm is not None:
            self._last_threshold = state.threshold_dbm
            if not dpg.is_item_active(TAG_THRESHOLD):
                dpg.set_value(TAG_THRESHOLD, state.threshold_dbm)
        for key, _label in _PRIMARY:
            dpg.set_value(f"markers.show.{key}", key not in state.hidden_traces)
        keys = list(state.references)
        dpg.configure_item("markers.freeze", enabled=len(keys) < MAX_REFERENCES)
        for slot in range(MAX_REFERENCES):
            ref_key = keys[slot] if slot < len(keys) else None
            self._ref_keys[slot] = ref_key
            dpg.configure_item(f"markers.ref.{slot}", show=ref_key is not None)
            if ref_key is not None:
                label = state.references[ref_key].label
                dpg.configure_item(f"markers.ref_show.{slot}", label=label)
                dpg.set_value(f"markers.ref_show.{slot}", ref_key not in state.hidden_traces)


__all__ = ["MarkersPanel", "delta_text", "level_text"]
