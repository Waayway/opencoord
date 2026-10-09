"""Analysis panel: channel overlay, amplitude offset, exclusion zones, carriers, occupancy."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.coord import channel_plans
from opencoord.core.analysis import MAX_CARRIER_ROWS, Analysis, CarrierRow
from opencoord.core.occupancy import PARTIAL_COVERAGE, ChannelOccupancy
from opencoord.core.settings import AMP_OFFSET_LIMIT_DB
from opencoord.core.zones import MAX_EXCLUSION_ZONES
from opencoord.ui import theme
from opencoord.ui.controller import Controller
from opencoord.ui.overlay import MAX_CHANNELS
from opencoord.ui.state import AppState

TAG_OVERLAY = "analysis.overlay"
TAG_PLAN = "analysis.plan"
TAG_OFFSET = "analysis.offset"
TAG_NEW_START = "analysis.zone.new_start"
TAG_NEW_STOP = "analysis.zone.new_stop"
_MHZ = 1e6


def carrier_text(row: CarrierRow) -> tuple[str, str, str]:
    """``(MHz, dBm, channel)`` cells of a detected carrier ("-" outside the plan)."""
    return (
        f"{row.carrier.freq_hz / _MHZ:.3f}",
        f"{row.carrier.level_dbm:.1f}",
        "-" if row.channel is None else str(row.channel),
    )


def occupancy_text(o: ChannelOccupancy) -> tuple[str, str, str, str, str]:
    """``(ch, max, avg, % above, coverage %)`` cells; a partly covered channel shows "partial"
    instead of a verdict."""
    pct = "partial" if o.coverage < PARTIAL_COVERAGE else f"{o.percent_above:.0f}"
    return (str(o.number), f"{o.max_dbm:.1f}", f"{o.avg_dbm:.1f}", pct, f"{o.coverage * 100:.0f}")


def analysis_summary(analysis: Analysis | None) -> str:
    if analysis is None:
        return "No trace yet. Start Live or a scan."
    return (
        f"{analysis.trace_label}: noise floor {analysis.floor_dbm:.1f} dBm, carriers above "
        f"{analysis.floor_dbm + analysis.threshold_db:.1f} dBm "
        f"({'threshold line' if analysis.from_threshold_line else 'floor + 10 dB'}), "
        f"{len(analysis.carriers)} found"
    )


class AnalysisPanel:
    def __init__(self, controller: Controller) -> None:
        self._c = controller
        self._versions = (-1, -1)
        self._analysis: Analysis | None = None
        self._ui_version = -1
        self._zone_ids: list[int | None] = [None] * MAX_EXCLUSION_ZONES
        self._carriers: list[CarrierRow | None] = [None] * MAX_CARRIER_ROWS
        #: Text last written to each cell, so unchanged cells cost no DPG call.
        self._text: dict[str, str] = {}

    def _set(self, tag: str, text: str) -> None:
        if self._text.get(tag) != text:
            self._text[tag] = text
            dpg.set_value(tag, text)

    @property
    def text_inputs(self) -> list[str]:
        tags = [TAG_OFFSET, TAG_NEW_START, TAG_NEW_STOP]
        for slot in range(MAX_EXCLUSION_ZONES):
            tags += [f"analysis.zone.start.{slot}", f"analysis.zone.stop.{slot}"]
        return tags

    # --- callbacks ---

    def _on_offset(self, _sender: object, value: float) -> None:
        self._c.set_amp_offset_db(float(value))
        self._ui_version = -1  # show the accepted (or the old) value again

    def _on_add_zone(self) -> None:
        start, stop = dpg.get_value(TAG_NEW_START), dpg.get_value(TAG_NEW_STOP)
        self._c.add_exclusion_zone(start * _MHZ, stop * _MHZ)

    def _on_zone_edit(self, _sender: object, _value: object, slot: int) -> None:
        zone_id = self._zone_ids[slot]
        if zone_id is not None:
            start = dpg.get_value(f"analysis.zone.start.{slot}")
            stop = dpg.get_value(f"analysis.zone.stop.{slot}")
            self._c.update_exclusion_zone(zone_id, start * _MHZ, stop * _MHZ)
            self._ui_version = -1

    def _on_zone_delete(self, _sender: object, _value: object, slot: int) -> None:
        zone_id = self._zone_ids[slot]
        if zone_id is not None:
            self._c.remove_exclusion_zone(zone_id)

    def _on_marker(self, _sender: object, _value: object, slot: int) -> None:
        row = self._carriers[slot]
        if row is not None:
            self._c.add_marker(row.carrier.freq_hz)

    # --- layout ---

    def build(self) -> None:
        c = self._c
        dpg.add_text("Channel overlay")
        dpg.add_checkbox(
            label="Show channel grid, bands and occupancy",
            tag=TAG_OVERLAY,
            callback=lambda _s, on: c.set_overlay_enabled(bool(on)),
        )
        dpg.add_combo(
            channel_plans.available(),
            tag=TAG_PLAN,
            label="Plan",
            width=120,
            callback=lambda _s, name: c.set_channel_plan(str(name)),
        )
        dpg.add_text(
            "Bands: green = allowed, red = forbidden, blue = info. Channel numbers are coloured "
            "by occupancy: green free, amber some bins above the threshold, red mostly occupied. "
            "Legality is best effort: check the current RDI / Agentschap Telecom rules.",
            wrap=330,
            color=theme.MUTED_COLOR,
        )
        dpg.add_separator()
        dpg.add_text("Amplitude offset")
        dpg.add_input_double(
            tag=TAG_OFFSET,
            label="dB",
            format="%.2f",
            step=0,
            on_enter=True,
            width=110,
            callback=self._on_offset,
        )
        dpg.add_text("", tag="analysis.offset_note", wrap=330, color=theme.MUTED_COLOR)
        dpg.add_separator()
        dpg.add_text("Exclusion zones")
        with dpg.group(horizontal=True):
            dpg.add_input_double(
                tag=TAG_NEW_START, format="%.3f", step=0, width=95, default_value=0.0
            )
            dpg.add_text("to")
            dpg.add_input_double(
                tag=TAG_NEW_STOP, format="%.3f", step=0, width=95, default_value=0.0
            )
            dpg.add_text("MHz")
            dpg.add_button(label="Add", tag="analysis.zone.add", callback=self._on_add_zone)
        dpg.add_text(
            "Frequencies to keep clear (dark areas on the plot). Edit a row and press Enter.",
            wrap=330,
            color=theme.MUTED_COLOR,
        )
        for slot in range(MAX_EXCLUSION_ZONES):
            with dpg.group(horizontal=True, tag=f"analysis.zone.row.{slot}", show=False):
                dpg.add_text("", tag=f"analysis.zone.id.{slot}")
                dpg.add_input_double(
                    tag=f"analysis.zone.start.{slot}",
                    format="%.3f",
                    step=0,
                    on_enter=True,
                    width=90,
                    callback=self._on_zone_edit,
                    user_data=slot,
                )
                dpg.add_input_double(
                    tag=f"analysis.zone.stop.{slot}",
                    format="%.3f",
                    step=0,
                    on_enter=True,
                    width=90,
                    callback=self._on_zone_edit,
                    user_data=slot,
                )
                dpg.add_button(
                    label="Delete",
                    small=True,
                    tag=f"analysis.zone.delete.{slot}",
                    callback=self._on_zone_delete,
                    user_data=slot,
                )
        dpg.add_separator()
        dpg.add_text("Detected carriers")
        dpg.add_text("", tag="analysis.summary", wrap=330, color=theme.MUTED_COLOR)
        with dpg.table(
            tag="analysis.carriers",
            header_row=True,
            borders_innerH=True,
            policy=dpg.mvTable_SizingStretchProp,
        ):
            dpg.add_table_column(label="MHz", init_width_or_weight=1.3)
            dpg.add_table_column(label="dBm", init_width_or_weight=1.0)
            dpg.add_table_column(label="Ch", init_width_or_weight=0.7)
            dpg.add_table_column(label="", init_width_or_weight=1.6)
            for slot in range(MAX_CARRIER_ROWS):
                with dpg.table_row(tag=f"analysis.carrier.row.{slot}", show=False):
                    dpg.add_text("", tag=f"analysis.carrier.freq.{slot}")
                    dpg.add_text("", tag=f"analysis.carrier.level.{slot}")
                    dpg.add_text("", tag=f"analysis.carrier.channel.{slot}")
                    dpg.add_button(
                        label="Add marker",
                        small=True,
                        tag=f"analysis.carrier.marker.{slot}",
                        callback=self._on_marker,
                        user_data=slot,
                    )
        with (
            dpg.collapsing_header(label="Channel occupancy", tag="analysis.occ.header"),
            dpg.table(
                tag="analysis.occ",
                header_row=True,
                borders_innerH=True,
                policy=dpg.mvTable_SizingStretchProp,
            ),
        ):
            dpg.add_table_column(label="Ch")
            dpg.add_table_column(label="Max dBm")
            dpg.add_table_column(label="Avg dBm")
            dpg.add_table_column(label="% above")
            dpg.add_table_column(label="Cov %")
            for i in range(MAX_CHANNELS):
                with dpg.table_row(tag=f"analysis.occ.row.{i}", show=False):
                    for col in ("ch", "max", "avg", "pct", "cov"):
                        dpg.add_text("", tag=f"analysis.occ.{col}.{i}")

    # --- per frame ---

    def update(self, state: AppState) -> None:
        if not dpg.is_item_visible("analysis.summary"):
            return  # another tab is showing: refresh when this one is (versions stay stale)
        versions = (state.ui_version, state.trace_version)
        if versions == self._versions:
            return
        self._versions = versions
        if state.ui_version != self._ui_version:
            self._ui_version = state.ui_version
            self._update_widgets(state)
        analysis = self._c.analysis()
        if analysis is not self._analysis or analysis is None:
            self._analysis = analysis
            self._update_tables(analysis)

    def _update_widgets(self, state: AppState) -> None:
        dpg.set_value(TAG_OVERLAY, state.overlay_enabled)
        if state.channel_plan is not None:
            dpg.set_value(TAG_PLAN, state.channel_plan.name)
        key = self._c.amp_offset_key()
        dpg.configure_item(TAG_OFFSET, enabled=key is not None)
        if not dpg.is_item_active(TAG_OFFSET):
            dpg.set_value(TAG_OFFSET, self._c.amp_offset_db)
        dpg.set_value(
            "analysis.offset_note",
            "Connect a device to set its offset."
            if key is None
            else f"Added to every sweep of this module ({key}); stored per model, not per unit. "
            f"Range +/-{AMP_OFFSET_LIMIT_DB:.0f} dB. Changing it clears the traces.",
        )
        zones = state.exclusion_zones
        for slot in range(MAX_EXCLUSION_ZONES):
            shown = slot < len(zones)
            dpg.configure_item(f"analysis.zone.row.{slot}", show=shown)
            self._zone_ids[slot] = zones[slot].id if shown else None
            if not shown:
                continue
            zone = zones[slot]
            self._set(f"analysis.zone.id.{slot}", f"X{zone.id}")
            for edge, hz in (("start", zone.start_hz), ("stop", zone.stop_hz)):
                tag = f"analysis.zone.{edge}.{slot}"
                if not dpg.is_item_active(tag):
                    dpg.set_value(tag, hz / _MHZ)
        dpg.configure_item("analysis.zone.add", enabled=len(zones) < MAX_EXCLUSION_ZONES)

    def _update_tables(self, analysis: Analysis | None) -> None:
        self._set("analysis.summary", analysis_summary(analysis))
        rows = analysis.carriers if analysis else ()
        for slot in range(MAX_CARRIER_ROWS):
            shown = slot < len(rows)
            dpg.configure_item(f"analysis.carrier.row.{slot}", show=shown)
            self._carriers[slot] = rows[slot] if shown else None
            if shown:
                freq, level, channel = carrier_text(rows[slot])
                self._set(f"analysis.carrier.freq.{slot}", freq)
                self._set(f"analysis.carrier.level.{slot}", level)
                self._set(f"analysis.carrier.channel.{slot}", channel)
        occupancy = analysis.occupancy if analysis else ()
        for i in range(MAX_CHANNELS):
            shown = i < len(occupancy)
            dpg.configure_item(f"analysis.occ.row.{i}", show=shown)
            if shown:
                cells = occupancy_text(occupancy[i])
                for col, text in zip(("ch", "max", "avg", "pct", "cov"), cells, strict=True):
                    self._set(f"analysis.occ.{col}.{i}", text)


__all__ = ["AnalysisPanel", "analysis_summary", "carrier_text", "occupancy_text"]
