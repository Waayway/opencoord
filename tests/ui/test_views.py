"""Pure helpers of the Dear PyGui views (no window needed)."""

from __future__ import annotations

import numpy as np

from opencoord.core.types import DeviceConfig, ModelInfo, Trace
from opencoord.device import models
from opencoord.device.link import SerialPort
from opencoord.device.scanner import Resolution, ScanProgress
from opencoord.ui import shortcuts
from opencoord.ui.app import status_line
from opencoord.ui.controller import Controller
from opencoord.ui.panels.device import AUTO_DETECT, SIMULATOR, connect_label, info_lines, port_items
from opencoord.ui.panels.scan import (
    estimate_text,
    progress_overlay,
    resolution_labels,
    run_label,
)
from opencoord.ui.spectrum import readout
from opencoord.ui.state import AppState
from opencoord.ui.waterfall import MIN_VISIBLE_ROWS, build_lut, to_rgba, visible_rows

MHZ = 1_000_000


def _config() -> DeviceConfig:
    return DeviceConfig(
        start_hz=470 * MHZ,
        step_hz=78_000,
        amp_top_dbm=-30,
        amp_bottom_dbm=-120,
        sweep_points=512,
        expansion_active=False,
        mode=0,
        min_hz=50_000,
        max_hz=960 * MHZ,
        max_span_hz=342_370_000,
        rbw_hz=95_000,
        amp_offset_db=0.0,
        calculator_mode=0,
    )


def _connected() -> AppState:
    st = AppState(connection="connected")
    st.model = ModelInfo(10, None, "03.39")
    st.config = _config()
    st.capabilities = models.resolve(st.model, st.config)
    return st


def test_readout_shows_cursor_and_nearest_trace_level() -> None:
    freqs = np.array([600e6, 600.1e6, 600.2e6])
    trace = Trace(freqs, np.array([-100, -50, -90], dtype=np.float32), "Max hold")
    assert readout(600.11, -70.0, None) == "600.110 MHz   -70.0 dBm"
    text = readout(600.11, -70.0, trace)
    assert "Max hold: -50.0 dBm at 600.100 MHz" in text
    assert readout(700.0, -70.0, trace) == "700.000 MHz   -70.0 dBm"  # outside the trace


def test_port_items() -> None:
    st = AppState()
    st.ports = [SerialPort("/dev/ttyUSB0", "CP2102N", True), SerialPort("/dev/ttyS0", "", False)]
    items = port_items(st)
    assert items[AUTO_DETECT] is None
    assert items["/dev/ttyUSB0 - CP2102N (RF Explorer)"] == "/dev/ttyUSB0"
    assert items["/dev/ttyS0"] == "/dev/ttyS0"
    assert port_items(AppState(simulator=True)) == {SIMULATOR: None}


def test_device_info_lines() -> None:
    assert info_lines(AppState())["status"] == "Status: Not connected"
    assert info_lines(AppState())["model"] == ""
    lines = info_lines(_connected())
    assert lines["model"] == "Model: RF Explorer WSUB1G+"
    assert lines["firmware"] == "Firmware: 03.39"
    assert lines["points"] == "Sweep points: 512 (max 4096)"
    assert "RBW 95 kHz" in lines["tuned"]
    assert connect_label(AppState()) == "Connect"
    assert connect_label(_connected()) == "Disconnect"


def test_scan_panel_texts() -> None:
    labels = resolution_labels()
    assert list(labels.values()) == [Resolution.FAST, Resolution.NORMAL, Resolution.FINE]
    assert "Normal (78 kHz bins)" in labels
    st = AppState()
    assert run_label(st) == "Start live"
    st.mode = "scan"
    assert run_label(st) == "Start scan"
    st.running = True
    assert run_label(st) == "Stop"
    st.running, st.stopping = False, True
    assert run_label(st) == "Stopping..."
    st.stopping = False
    assert "connect" in estimate_text(st)
    st.scan_estimate_s = 58.4
    assert estimate_text(st) == "Estimated scan time: 58 s"
    st.scan_estimate_s = 120
    assert estimate_text(st) == "Estimated scan time: 2.0 min"
    assert progress_overlay(st) == (0.0, "")
    st.scan_progress = ScanProgress(2, 13, 0.2, None, False)
    assert progress_overlay(st) == (0.2, "Segment 3 / 13")


def test_status_line() -> None:
    st = _connected()
    st.running = True
    st.sweeps_per_s = 0.9
    st.message = "Live"
    st.view_range_hz = (470 * MHZ, 812_370_000)
    text = status_line(st)
    assert text.startswith("Connected")
    assert "470.000 - 812.370 MHz" in text
    assert "0.9 sweeps/s" in text and "RBW 95 kHz" in text


def test_lut_and_colour_mapping() -> None:
    lut = build_lut(256)
    assert lut.shape == (257, 4) and lut.dtype == np.float32
    assert (lut[:, 3] == 1).all()
    brightness = lut[:256, :3].sum(axis=1)
    assert brightness[-1] > brightness[0]
    rows = np.array([[-200.0, -115.0, -35.0, 0.0, np.nan]], dtype=np.float32)
    rgba = to_rgba(rows, -115.0, -35.0, lut)
    assert rgba.shape == (1, 5, 4)
    np.testing.assert_array_equal(rgba[0, 0], lut[0])
    np.testing.assert_array_equal(rgba[0, 1], lut[0])
    np.testing.assert_array_equal(rgba[0, 2], lut[255])
    np.testing.assert_array_equal(rgba[0, 3], lut[255])
    np.testing.assert_array_equal(rgba[0, 4], lut[256])  # no data: background


def test_visible_rows() -> None:
    assert visible_rows(0, 300) == MIN_VISIBLE_ROWS
    assert visible_rows(150, 300) == 150
    assert visible_rows(300, 300) == 300
    assert visible_rows(0, 5) == 5


def test_shortcuts_map_to_controller_intents() -> None:
    by_label = {s.label: s.action for s in shortcuts.SHORTCUTS}
    assert list(by_label) == ["Space", "R", "M", "P", "N", "Shift+N"]
    assert by_label["Space"] is Controller.toggle
    assert by_label["R"] is Controller.reset_max_hold
    assert by_label["M"] is Controller.add_marker_at_cursor
    assert by_label["P"] is Controller.marker_to_peak
    assert shortcuts.help_text().startswith("Space: Start / stop   R: Reset max hold   M: ")


def test_next_peak_shortcuts_go_right_and_left() -> None:
    from opencoord.core.types import Sweep
    from opencoord.device.simulator import SimulatedLink

    c = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [])
    dbm = np.asarray([-100, -60, -100, -100, -70, -100], dtype=np.float32)
    c.state.traces.update(Sweep(470e6 + 100e3 * np.arange(6.0), dbm, 0.0))
    c.add_marker(470_100_000)
    right = next(s for s in shortcuts.SHORTCUTS if s.key == "N" and not s.shift)
    left = next(s for s in shortcuts.SHORTCUTS if s.key == "N" and s.shift)
    right.action(c)
    assert c.state.markers[0].freq_hz == 470_400_000
    left.action(c)
    assert c.state.markers[0].freq_hz == 470_100_000


def test_marker_text_and_delta_text() -> None:
    from opencoord.core.markers import Marker
    from opencoord.ui.controller import MarkerRow
    from opencoord.ui.panels.markers import delta_text, level_text
    from opencoord.ui.spectrum import marker_text

    row = MarkerRow(Marker(1, 612_350_000, "max"), -67.24, None)
    assert marker_text(row) == "M1 612.350 MHz -67.2 dBm"
    assert level_text(row) == "-67.2"
    out = MarkerRow(Marker(2, 612_350_000, "max"), None, None)
    assert marker_text(out) == "M2 612.350 MHz"
    assert level_text(out) == "-"
    assert delta_text(None) == ""
    assert delta_text((600_000, 10.04)) == "+0.600 MHz +10.0 dB"
    assert delta_text((-1_250_000, -3.0)) == "-1.250 MHz -3.0 dB"


# --- analysis / overlay / module helpers ---------------------------------------------------------


def test_occupancy_colour_steps() -> None:
    from opencoord.ui.overlay import (
        BUSY_COLOR,
        FREE_COLOR,
        PLAIN_COLOR,
        SOME_COLOR,
        occupancy_color,
    )

    assert occupancy_color(None) == PLAIN_COLOR
    assert occupancy_color(0.0) == FREE_COLOR
    assert occupancy_color(5.0) == SOME_COLOR
    assert occupancy_color(25.0) == BUSY_COLOR


def test_carrier_text_and_summary() -> None:
    from opencoord.core.types import Carrier
    from opencoord.ui.controller import Analysis, CarrierRow
    from opencoord.ui.panels.analysis import analysis_summary, carrier_text

    assert carrier_text(CarrierRow(Carrier(474_500_000, -51.23), 21)) == ("474.500", "-51.2", "21")
    assert carrier_text(CarrierRow(Carrier(900_000_000, -60.0), None))[2] == "-"
    assert "No trace" in analysis_summary(None)
    a = Analysis(("k",), "Max hold", -100.0, 10.0, False, (), ())
    assert "floor + 10 dB" in analysis_summary(a)
    assert "threshold line" in analysis_summary(
        Analysis(("k",), "Max hold", -100.0, 5.0, True, (), ())
    )


def test_module_labels_only_with_an_expansion() -> None:
    from dataclasses import replace

    from opencoord.ui.panels.device import module_labels

    st = AppState()
    assert module_labels(st) is None
    caps = models.resolve(ModelInfo(10, None, "03.39"), _config())
    st.capabilities = caps
    assert module_labels(st) is None
    st.capabilities = replace(caps, expansion_name="RF Explorer 2.4G")
    assert module_labels(st) == ("RF Explorer WSUB1G+ (active)", "RF Explorer 2.4G")
    st.capabilities = replace(caps, expansion_name="RF Explorer 2.4G", expansion=True)
    assert module_labels(st) == ("RF Explorer WSUB1G+", "RF Explorer 2.4G (active)")
