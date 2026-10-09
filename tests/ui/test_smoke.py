"""UI smoke tests: real Dear PyGui window against the simulator (needs a display).

Dear PyGui cannot reliably create a second viewport in one process (it segfaults), so only one
test here opens a window in-process; ``--smoke-frames`` runs in a subprocess.
"""

import logging
import os
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(
        not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="no display available",
    ),
]

MHZ = 1_000_000
log = logging.getLogger(__name__)


def test_smoke_frames() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "opencoord", "--smoke-frames", "5"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rendered 5 frames" in result.stderr


def test_live_and_scan_against_the_simulator() -> None:
    import dearpygui.dearpygui as dpg

    from opencoord.core.settings import AppSettings
    from opencoord.device.scanner import Resolution
    from opencoord.device.simulator import SimulatedLink
    from opencoord.ui.app import App
    from opencoord.ui.controller import Controller

    c = Controller(
        lambda _port: SimulatedLink(sweep_points=512, sweep_interval_s=0.02),
        port_lister=lambda: [],
        simulator=True,
    )
    app = App(c, AppSettings())
    app.build()
    try:
        deadline = time.monotonic() + 5
        while c.state.connection != "connected":
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        c.start()
        t0 = time.perf_counter()
        for _ in range(60):
            assert app.frame()
        live_fps = 60 / (time.perf_counter() - t0)
        live = c.state.traces.live
        assert live is not None and len(live.freqs_hz) == 512
        assert c.state.waterfall.count > 0
        x, y = dpg.get_value("spectrum.trace.live")[:2]
        assert len(x) == len(y) == 512
        assert len(dpg.get_value("spectrum.trace.max")[0]) == 512

        c.set_mode("scan")
        c.set_resolution(Resolution.FAST)
        c.set_range(470 * MHZ, 500 * MHZ)
        c.start()
        deadline = time.monotonic() + 8
        while c.state.busy:
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert "Scan done" in c.state.message
        scan = c.state.traces.live
        assert scan is not None and scan.start_hz == pytest.approx(470 * MHZ, abs=MHZ)
        assert len(dpg.get_value("spectrum.trace.live")[0]) == len(scan.freqs_hz)
        for tab in ("markers", "coordination", "profiles"):
            assert dpg.does_item_exist(f"tab.{tab}")
        stats = app.stats()
        log.info(
            "smoke: live %.0f fps; overall %d frames, %.0f fps, work %.2f ms mean / %.2f ms max",
            live_fps,
            stats.frames,
            stats.fps,
            stats.work_ms_mean,
            stats.work_ms_max,
        )
    finally:
        app.close()
