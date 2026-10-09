"""Regenerate the README screenshots in ``docs/screenshots/`` from the simulator.

    uv run python docs/make_screenshots.py            # needs a display (or xvfb-run -a)

Drives the real app (``opencoord.ui.app.App``) through a Fast scan of 470-700 MHz and a live run,
then saves the spectrum + waterfall, the Analysis overlay and the Coordination tab. Everything runs
against the simulated RF Explorer and a throw-away profile folder, never real settings.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from opencoord.coord.profiles import TuningRange, builtin_templates
from opencoord.core.settings import AppSettings
from opencoord.device.scanner import Resolution
from opencoord.device.simulator import SimulatedLink
from opencoord.io.profile_store import ProfileStore
from opencoord.ui.app import App
from opencoord.ui.controller import Controller

MHZ = 1_000_000
OUT = Path(__file__).resolve().parent / "screenshots"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    store = ProfileStore(Path(tempfile.mkdtemp(prefix="opencoord-shots-")))
    template = builtin_templates()["generic-analog-mic"]
    store.save_profile(
        replace(template, name="UHF mic", tuning=(TuningRange(470 * MHZ, 694 * MHZ),))
    )
    controller = Controller(
        lambda _port: SimulatedLink(sweep_points=512, sweep_interval_s=0.01),
        port_lister=lambda: [],
        simulator=True,
    )
    app = App(
        controller,
        AppSettings(window_width=1500, window_height=950),
        profile_store=store,
    )
    app.build()

    def frames(n: int) -> None:
        for _ in range(n):
            app.frame()

    def shoot(name: str, tab: str) -> None:
        app.select_tab(tab)
        app.capture_window(OUT / name)
        while not app.capture_done:
            app.frame()
        if not app.capture_ok:
            sys.exit(f"could not save {name}")
        print("wrote", OUT / name)

    try:
        while controller.state.connection != "connected":
            app.frame()
        controller.set_mode("scan")
        controller.set_resolution(Resolution.FAST)
        controller.set_range(470 * MHZ, 700 * MHZ)
        controller.start()
        while controller.state.busy:
            app.frame()
        controller.set_mode("live")
        controller.start()
        frames(500)  # fills the waterfall

        controller.stop()  # a frozen max hold, so the plan below is not "stale"
        frames(30)
        controller.add_marker_at_peak()
        controller.marker_next_peak("right")
        frames(30)
        shoot("spectrum.png", "markers")

        controller.set_channel_plan("eu")
        controller.set_overlay_enabled(True)
        controller.set_threshold_dbm(-90.0)
        controller.add_exclusion_zone(606 * MHZ, 614 * MHZ)
        frames(60)
        shoot("analysis.png", "analysis")

        coordination = app.coordination
        coordination.model.add_device("UHF mic", 8)
        coordination.model.paste_locks("606.5 612.25", label="TV link")
        frames(3)
        coordination.coordinate()
        while coordination.running:
            app.frame()
        frames(40)
        shoot("coordination.png", "coordination")
    finally:
        app.close()


if __name__ == "__main__":
    main()
