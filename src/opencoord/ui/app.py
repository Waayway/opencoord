"""Dear PyGui application entry point (``opencoord``).

Layout: a toolbar (port + connect, mode, preset, resolution, start/stop, reset max hold), the
spectrum plot over the waterfall (x axes linked), a right-hand tab bar (Device | Scan | Markers |
Analysis | Record | Coordination | Profiles) and a status bar. The frame loop is manual: each
frame calls ``Controller.tick()``, lets the views push changed data to Dear PyGui, then renders.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import dearpygui.dearpygui as dpg
import numpy as np

from opencoord import __version__
from opencoord.core import settings as settings_io
from opencoord.core.settings import AppSettings
from opencoord.device.link import SerialLink, find_ports
from opencoord.device.link_api import Link
from opencoord.device.simulator import SimulatedLink
from opencoord.io.atomic import write_atomic
from opencoord.io.png import encode_png
from opencoord.io.profile_store import ProfileStore
from opencoord.ui import shortcuts, theme
from opencoord.ui.controller import Controller, LinkFactory
from opencoord.ui.coordination_actions import CoordinationActions
from opencoord.ui.file_dialogs import FileUI, frame_to_rgba
from opencoord.ui.files import FileActions
from opencoord.ui.panels import device as device_panel
from opencoord.ui.panels import scan as scan_panel
from opencoord.ui.panels.analysis import AnalysisPanel
from opencoord.ui.panels.coordination import CoordinationPanel
from opencoord.ui.panels.device import DevicePanel
from opencoord.ui.panels.markers import MarkersPanel
from opencoord.ui.panels.profiles import ProfilesPanel
from opencoord.ui.panels.record import RecordPanel
from opencoord.ui.panels.scan import ScanPanel
from opencoord.ui.plan_overlay import PlanOverlayView
from opencoord.ui.profiles_actions import ProfilesActions
from opencoord.ui.recording import RecordingActions
from opencoord.ui.spectrum import TAG_READOUT, TAG_X, TAG_Y, SpectrumView
from opencoord.ui.state import AppState
from opencoord.ui.waterfall import WaterfallView

log = logging.getLogger(__name__)

#: The simulator runs at 512 points per sweep like the WSUB1G+ in Normal/Fine scans.
SIMULATOR_POINTS = 512
PANEL_WIDTH = 370
STATUS_HEIGHT = 30
#: Horizontal gap between the plot area and the right-hand panel (window padding + spacing).
PLOT_MARGIN = 8


#: Side-panel tabs by name (``--tab``).
TABS: Final = {
    name: f"tab.{name}"
    for name in ("device", "scan", "markers", "analysis", "record", "coordination", "profiles")
}
#: ``--screenshot`` waits at least this long (sweeps must fill the waterfall) and this many frames.
SCREENSHOT_SETTLE_S: Final = 4.0
SCREENSHOT_MIN_FRAMES: Final = 120
#: Frames to wait for the frame-buffer callback before giving up.
SCREENSHOT_TIMEOUT_FRAMES: Final = 120


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencoord", description="OpenCoord spectrum scanner")
    parser.add_argument(
        "session",
        nargs="?",
        type=Path,
        default=None,
        help="session file (.opencoord) to open at startup",
    )
    parser.add_argument("--version", action="version", version=f"opencoord {__version__}")
    parser.add_argument(
        "--simulator", action="store_true", help="use the simulated RF Explorer instead of USB"
    )
    parser.add_argument(
        "--smoke-frames",
        type=int,
        metavar="N",
        default=None,
        help="render N frames, then exit 0 (CI and packaging smoke test)",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        metavar="PATH",
        default=None,
        help="render the window for a few seconds, save it as a PNG at PATH and exit "
        "(documentation screenshots; combine with --simulator)",
    )
    parser.add_argument(
        "--tab",
        choices=sorted(TABS),
        default=None,
        help="side-panel tab to show at startup",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output")
    return parser


def link_factory(simulator: bool) -> LinkFactory:
    if simulator:

        def make_sim(_port: str | None) -> Link:
            return SimulatedLink(sweep_points=SIMULATOR_POINTS)

        return make_sim

    def make_serial(port: str | None) -> Link:
        return SerialLink(port)

    return make_serial


def status_line(state: AppState) -> str:
    conn = {
        "disconnected": "Disconnected",
        "connecting": "Connecting",
        "connected": "Connected",
        "reconnecting": "Reconnecting",
        "disconnecting": "Disconnecting",
    }[state.connection]
    parts = [conn]
    if state.simulator:
        parts[0] += " (simulator)"
    if state.running:
        parts.append("Live" if state.mode == "live" else "Scanning")
    lo, hi = state.view_range_hz
    parts.append(f"{lo / 1e6:.3f} - {hi / 1e6:.3f} MHz")
    cfg = state.config
    if cfg is not None and state.mode == "live":
        rbw = f", RBW {cfg.rbw_hz / 1e3:.0f} kHz" if cfg.rbw_hz else ""
        parts.append(f"step {cfg.step_hz / 1e3:.1f} kHz{rbw}")
    if state.running and state.mode == "live":
        parts.append(f"{state.sweeps_per_s:.1f} sweeps/s")
    if state.logger_alert:
        parts.append(state.logger_alert)
    parts.append(state.message)
    return "   |   ".join(parts)


@dataclass(frozen=True)
class FrameStats:
    frames: int
    seconds: float
    #: Mean time per frame spent in tick() and the view updates (not rendering).
    work_ms_mean: float
    work_ms_max: float

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds > 0 else 0.0


class App:
    def __init__(
        self,
        controller: Controller,
        settings: AppSettings,
        session: Path | None = None,
        *,
        profile_store: ProfileStore,
    ) -> None:
        self.controller = controller
        self.files = FileActions(controller)
        self.file_ui = FileUI(self.files, self.plot_rect)
        self._session_arg = session
        self._settings = settings
        self.device_panel = DevicePanel(controller)
        self.scan_panel = ScanPanel(controller)
        self.markers_panel = MarkersPanel(controller)
        self.analysis_panel = AnalysisPanel(controller)
        self.recording = RecordingActions(controller)
        self.record_panel = RecordPanel(controller, self.recording, self.file_ui.ask)
        self.profiles = ProfilesActions(profile_store, self.files.say)
        self.profiles_panel = ProfilesPanel(self.profiles, self.file_ui.ask)
        self.coordination = CoordinationActions(controller, self.profiles, self.files.say)
        self.files.coordination = self.coordination
        self.coordination_panel = CoordinationPanel(
            self.coordination, self.file_ui.ask, self.file_ui.capture_plot
        )
        self.spectrum = SpectrumView(controller, PlanOverlayView(self.coordination, TAG_X, TAG_Y))
        self.waterfall = WaterfallView()
        self._status_version = -1
        self._frames = 0
        self._started = 0.0
        self._work_total = 0.0
        self._work_max = 0.0
        self._built = False
        self._capture_cb: Callable[[object, Any], None] | None = None
        self._capture_wait = 0
        self.capture_done = False
        self.capture_ok = False

    # --- lifecycle ---

    def build(self) -> None:
        title = f"OpenCoord {__version__}"
        dpg.create_context()
        # Run every item/handler callback on this thread, from frame(), never on DPG's own thread.
        dpg.configure_app(manual_callback_management=True)
        self._built = True
        theme.apply()
        with dpg.value_registry():
            dpg.add_string_value(tag=device_panel.PORT_CHOICE)
            dpg.add_string_value(tag=scan_panel.PRESET_VALUE)
            dpg.add_string_value(tag=scan_panel.RESOLUTION_VALUE)
        with dpg.window(tag="main", label=title, menubar=True):
            with dpg.menu_bar():
                self.file_ui.build_menu()
            self._build_toolbar()
            dpg.add_separator()
            with dpg.group(horizontal=True):
                with dpg.child_window(width=-PANEL_WIDTH, height=-STATUS_HEIGHT, border=False):
                    dpg.add_text("", tag=TAG_READOUT)
                    with dpg.subplots(
                        2,
                        1,
                        link_all_x=True,
                        row_ratios=[3.0, 2.0],
                        width=-1,
                        height=-1,
                        no_title=True,
                        tag="plots",
                    ):
                        self.spectrum.build()
                        self.waterfall.build(self.controller.state.waterfall)
                with dpg.child_window(width=-1, height=-STATUS_HEIGHT), dpg.tab_bar(tag="tabs"):
                    with dpg.tab(label="Device", tag="tab.device"):
                        self.device_panel.build()
                    with dpg.tab(label="Scan", tag="tab.scan"):
                        self.scan_panel.build()
                    with dpg.tab(label="Markers", tag="tab.markers"):
                        self.markers_panel.build()
                    with dpg.tab(label="Analysis", tag="tab.analysis"):
                        self.analysis_panel.build()
                    with dpg.tab(label="Record", tag="tab.record"):
                        self.record_panel.build()
                    with dpg.tab(label="Coordination", tag="tab.coordination"):
                        self.coordination_panel.build()
                    with dpg.tab(label="Profiles", tag="tab.profiles"):
                        self.profiles_panel.build()
            dpg.add_text("", tag="status.line")
        self.file_ui.build_windows()
        dpg.set_primary_window("main", True)
        s = self._settings
        dpg.create_viewport(title=title, width=s.window_width, height=s.window_height)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        shortcuts.bind(
            self.controller,
            [
                *self.device_panel.text_inputs,
                *self.scan_panel.text_inputs,
                *self.markers_panel.text_inputs,
                *self.analysis_panel.text_inputs,
                *self.record_panel.text_inputs,
                *self.profiles_panel.text_inputs,
                *self.coordination_panel.text_inputs,
            ],
            typing=lambda: self.profiles_panel.is_typing() or self.coordination_panel.is_typing(),
        )
        shortcuts.bind_files(
            {
                "save": self.file_ui.save,
                "save_as": self.file_ui.save_as,
                "open": self.file_ui.open_session,
                "export": self.file_ui.export_dialog,
            }
        )
        self.controller.startup()
        self.profiles.startup()
        if self._session_arg is not None:
            try:
                opened = self.files.open(self._session_arg)
            except Exception:  # a bad file must never abort the start
                log.exception("unexpected error opening %s", self._session_arg)
                opened = False
            if not opened:
                log.warning(
                    "could not open %s: %s", self._session_arg, self.controller.state.message
                )
        self._started = time.perf_counter()

    def _build_toolbar(self) -> None:
        c = self.controller
        with dpg.group(horizontal=True, tag="toolbar"):
            dpg.add_combo(tag="toolbar.port_combo", source=device_panel.PORT_CHOICE, width=230)
            dpg.add_button(
                label="Connect",
                tag="toolbar.connect",
                width=110,
                callback=self.device_panel.connect_or_disconnect,
            )
            dpg.add_spacer(width=12)
            dpg.add_radio_button(
                list(scan_panel.MODES),
                tag="toolbar.mode",
                horizontal=True,
                callback=self.scan_panel.on_mode,
            )
            dpg.add_combo(
                tag="toolbar.preset",
                source=scan_panel.PRESET_VALUE,
                width=180,
                callback=self.scan_panel.on_preset,
            )
            dpg.add_combo(
                list(scan_panel.resolution_labels()),
                tag="toolbar.resolution",
                source=scan_panel.RESOLUTION_VALUE,
                width=180,
                callback=self.scan_panel.on_resolution,
            )
            dpg.add_button(label="Start", tag="toolbar.run", width=110, callback=lambda: c.toggle())
            dpg.add_button(
                label="Reset max hold", tag="toolbar.reset", callback=lambda: c.reset_max_hold()
            )

    def run_callbacks(self) -> None:
        """Run the callbacks DPG queued since the last frame (UI thread, manual management)."""
        jobs = dpg.get_callback_queue()
        if jobs:
            dpg.run_callbacks(jobs)

    def plot_rect(self) -> tuple[float, float, float, float]:
        """``(x, y, width, height)`` of the plot area (readout line, spectrum, waterfall)."""
        x, y = dpg.get_item_rect_min(TAG_READOUT)
        width = dpg.get_viewport_client_width() - x - PANEL_WIDTH - 2 * PLOT_MARGIN
        height = dpg.get_viewport_client_height() - y - STATUS_HEIGHT
        return x, y, width, height

    def _run_screenshot(self, path: Path) -> None:
        """Render until the sweeps have filled the plots, then save the window and stop."""
        started = False
        while self.frame():
            state = self.controller.state
            if not started and state.connection == "connected" and not state.running:
                started = True
                self.controller.start()  # nothing else would fill the plots
            settled = time.perf_counter() - self._started >= SCREENSHOT_SETTLE_S
            if settled and self._frames >= SCREENSHOT_MIN_FRAMES:
                break
        self.capture_window(path)
        waited = 0
        while not self.capture_done and waited < SCREENSHOT_TIMEOUT_FRAMES and self.frame():
            waited += 1
        if not self.capture_ok:
            raise RuntimeError(f"could not save the screenshot to {path}")

    def frame(self) -> bool:
        """Tick, update the views and render one frame; ``False`` once the window was closed."""
        if not dpg.is_dearpygui_running():
            return False
        t0 = time.perf_counter()
        self.controller.tick()
        self.run_callbacks()
        state = self.controller.state
        self.device_panel.update(state)
        self.scan_panel.update(state)
        self.markers_panel.update(state)
        self.analysis_panel.update(state)
        self.record_panel.update(state)
        self.profiles_panel.update()
        self.coordination_panel.update()
        self.spectrum.update(state)
        self.waterfall.update(state)
        self.file_ui.update()
        self._step_capture()
        if state.ui_version != self._status_version or self._frames % 30 == 0:
            self._status_version = state.ui_version
            fps = dpg.get_frame_rate()
            dpg.set_value("status.line", f"{status_line(state)}   |   {fps:.0f} fps")
        work = time.perf_counter() - t0
        self._work_total += work
        self._work_max = max(self._work_max, work)
        dpg.render_dearpygui_frame()
        self._frames += 1
        return True

    def stats(self) -> FrameStats:
        frames = max(self._frames, 1)
        return FrameStats(
            self._frames,
            time.perf_counter() - self._started,
            self._work_total / frames * 1e3,
            self._work_max * 1e3,
        )

    def window_settings(self, base: AppSettings) -> AppSettings:
        if not self._built:
            return base
        width, height = dpg.get_viewport_width(), dpg.get_viewport_height()
        if width < 400 or height < 400:  # minimised or not yet shown
            return base
        return replace(base, window_width=width, window_height=height)

    def close(self) -> None:
        self.controller.shutdown()
        if self._built:
            dpg.destroy_context()
            self._built = False

    def select_tab(self, name: str) -> None:
        """Show side-panel tab ``name`` (a key of :data:`TABS`)."""
        dpg.set_value("tabs", TABS[name])

    def capture_window(self, path: Path) -> None:
        """Save the whole rendered window to ``path`` as PNG a few frames from now; see
        :attr:`capture_done` and :attr:`capture_ok`."""
        self.capture_done = False
        self.capture_ok = False
        self._capture_wait = 4  # frames, so the tab switch and any dialog are rendered

        def saved(_sender: object, buffer: Any) -> None:
            try:
                rgba = frame_to_rgba(buffer, dpg.get_viewport_client_width())
                write_atomic(path, encode_png(np.ascontiguousarray(rgba)))
                self.capture_ok = True
            except (ValueError, KeyError, SystemError, OSError):
                log.exception("could not save the screenshot to %s", path)
            self.capture_done = True

        self._capture_cb = saved

    def _step_capture(self) -> None:
        if self._capture_cb is None:
            return
        if self._capture_wait > 0:
            self._capture_wait -= 1
            return
        callback, self._capture_cb = self._capture_cb, None
        dpg.output_frame_buffer(callback=callback)

    def run(
        self,
        max_frames: int | None = None,
        *,
        screenshot: Path | None = None,
        tab: str | None = None,
    ) -> FrameStats:
        self.build()
        try:
            try:
                if tab is not None:
                    self.select_tab(tab)
                if screenshot is not None:
                    self._run_screenshot(screenshot)
                while screenshot is None and (max_frames is None or self._frames < max_frames):
                    if not self.frame():
                        break
            except KeyboardInterrupt:  # Ctrl+C in the terminal: exit normally, save settings
                log.info("interrupted")
            stats = self.stats()
            log.info(
                "rendered %d frames in %.1f s (%.1f fps); per-frame work %.2f ms mean, %.2f ms max",
                stats.frames,
                stats.seconds,
                stats.fps,
                stats.work_ms_mean,
                stats.work_ms_max,
            )
            self._settings = self.window_settings(self._settings)
            return stats
        finally:
            self.close()

    @property
    def final_settings(self) -> AppSettings:
        """Settings to save on exit: the user's choices and the last window size."""
        return self.controller.current_settings(self._settings)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    smoke = args.smoke_frames is not None or args.screenshot is not None
    settings = settings_io.load()
    if smoke:  # CI / packaging: never touch a real device
        settings = replace(settings, auto_connect=False)
    controller = Controller(
        link_factory(args.simulator),
        settings=settings,
        port_lister=(lambda: []) if args.simulator else find_ports,
        simulator=args.simulator,
    )
    # The smoke run must not touch the user's profile folders.
    scratch = tempfile.TemporaryDirectory(prefix="opencoord-smoke-") if smoke else None
    try:
        store = ProfileStore(Path(scratch.name) if scratch else None)
        app = App(controller, settings, args.session, profile_store=store)
        app.run(max_frames=args.smoke_frames, screenshot=args.screenshot, tab=args.tab)
    finally:
        if scratch is not None:
            scratch.cleanup()
    if not smoke:
        try:
            settings_io.save(app.final_settings)
        except OSError:
            log.warning("could not save the settings", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
