"""Device panel: port choice, connect / disconnect, model and capabilities, errors."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.ui import theme
from opencoord.ui.controller import Controller
from opencoord.ui.state import AppState

#: Shared value of the port combos (device panel and toolbar).
PORT_CHOICE = "ui.port_choice"
AUTO_DETECT = "Auto-detect (RF Explorer USB)"
SIMULATOR = "Simulator"
TAG_MANUAL = "device.port_manual"
TAG_CONNECT = "device.connect"
_INFO = ("status", "model", "firmware", "range", "span", "points", "tuned", "expansion")


def port_items(state: AppState) -> dict[str, str | None]:
    """Combo labels mapped to the port to open (``None`` = auto-detect / simulator)."""
    if state.simulator:
        return {SIMULATOR: None}
    items: dict[str, str | None] = {AUTO_DETECT: None}
    for p in state.ports:
        label = p.device + (f" - {p.description}" if p.description else "")
        if p.is_rf_explorer:
            label += " (RF Explorer)"
        items[label] = p.device
    return items


def connect_label(state: AppState) -> str:
    return {
        "disconnected": "Connect",
        "connecting": "Connecting...",
        "connected": "Disconnect",
        "reconnecting": "Disconnect",
        "disconnecting": "Disconnecting...",
    }[state.connection]


def info_lines(state: AppState) -> dict[str, str]:
    caps, model, config = state.capabilities, state.model, state.config
    status = {
        "disconnected": "Not connected",
        "connecting": "Connecting...",
        "connected": "Connected" + (f" to {state.port}" if state.port else ""),
        "reconnecting": "Connection lost, reconnecting...",
        "disconnecting": "Disconnecting (closing the port)...",
    }[state.connection]
    lines = dict.fromkeys(_INFO, "")
    lines["status"] = f"Status: {status}"
    if caps is None:
        return lines
    lines["model"] = f"Model: {caps.name}"
    if model is not None:
        lines["firmware"] = f"Firmware: {model.firmware}"
    lines["range"] = f"Range: {caps.min_hz / 1e6:.3f} - {caps.max_hz / 1e6:.3f} MHz"
    lines["span"] = f"Max span: {caps.max_span_hz / 1e6:.2f} MHz (at the current sweep points)"
    if config is not None:
        lines["points"] = f"Sweep points: {config.sweep_points} (max {caps.sweep_points_max})"
        rbw = f", RBW {config.rbw_hz / 1e3:.0f} kHz" if config.rbw_hz else ""
        lines["tuned"] = (
            f"Tuned: {config.start_hz / 1e6:.3f} - {config.stop_hz / 1e6:.3f} MHz, "
            f"step {config.step_hz / 1e3:.1f} kHz{rbw}"
        )
    lines["expansion"] = f"Expansion module: {caps.expansion_name or 'none'}"
    return lines


class DevicePanel:
    def __init__(self, controller: Controller) -> None:
        self._c = controller
        self._version = -1
        self._items: dict[str, str | None] = {}

    @property
    def text_inputs(self) -> list[str]:
        return [TAG_MANUAL]

    def build(self) -> None:
        st = self._c.state
        dpg.add_text("Port")
        with dpg.group(horizontal=True):
            dpg.add_combo(tag="device.port_combo", source=PORT_CHOICE, width=-80)
            dpg.add_button(
                label="Refresh", tag="device.refresh", callback=lambda: self._c.refresh_ports()
            )
        dpg.add_input_text(
            tag=TAG_MANUAL,
            hint="or type a port: /dev/ttyUSB0, COM3 ...",
            width=-1,
            show=not st.simulator,
        )
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Connect", tag=TAG_CONNECT, width=110, callback=self.connect_or_disconnect
            )
            dpg.add_checkbox(
                label="Connect on start",
                tag="device.auto_connect",
                default_value=st.auto_connect,
                show=not st.simulator,
                callback=lambda _s, value: self._c.set_auto_connect(bool(value)),
            )
        dpg.add_separator()
        for key in _INFO:
            dpg.add_text("", tag=f"device.info.{key}")
        dpg.add_separator()
        dpg.add_text("", tag="device.error", color=theme.ERROR_COLOR, wrap=330)

    def selected_port(self) -> str | None:
        manual = str(dpg.get_value(TAG_MANUAL) or "").strip()
        if manual and not self._c.state.simulator:
            return manual
        return self._items.get(str(dpg.get_value(PORT_CHOICE)))

    def connect_or_disconnect(self) -> None:
        if self._c.state.connection == "disconnected":
            self._c.connect(self.selected_port())
        elif self._c.state.connection not in ("connecting", "disconnecting"):
            self._c.disconnect()

    def update(self, state: AppState) -> None:
        if state.ui_version == self._version:
            return
        self._version = state.ui_version
        items = port_items(state)
        if items != self._items:
            self._items = items
            labels = list(items)
            for tag in ("device.port_combo", "toolbar.port_combo"):
                if dpg.does_item_exist(tag):
                    dpg.configure_item(tag, items=labels)
            if dpg.get_value(PORT_CHOICE) not in items:
                rfe = [label for label, port in items.items() if port and "(RF Explorer)" in label]
                dpg.set_value(PORT_CHOICE, rfe[0] if rfe else labels[0])
        busy = state.connection in ("connecting", "disconnecting")
        for tag in (TAG_CONNECT, "toolbar.connect"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, label=connect_label(state), enabled=not busy)
        locked = state.connection != "disconnected"
        for tag in ("device.port_combo", "toolbar.port_combo", TAG_MANUAL):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=not locked)
        dpg.set_value("device.auto_connect", state.auto_connect)
        for key, line in info_lines(state).items():
            dpg.set_value(f"device.info.{key}", line)
        dpg.set_value("device.error", state.error or "")


__all__ = ["DevicePanel", "connect_label", "info_lines", "port_items"]
