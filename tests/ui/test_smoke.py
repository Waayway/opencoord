"""UI smoke tests: real Dear PyGui window against the simulator (needs a display).

Dear PyGui cannot reliably create a second viewport in one process (it segfaults), so only one
test here opens a window in-process; ``--smoke-frames`` runs in a subprocess.
"""

import inspect
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

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


def test_opens_session_argument(tmp_path: Path) -> None:
    from opencoord.core import session as session_io

    good = tmp_path / "ok.opencoord"
    session_io.save(good, session_io.Session())
    bad = tmp_path / "bad.opencoord"
    bad.write_bytes(b"nope")
    for path, warned in ((good, False), (bad, True)):
        result = subprocess.run(
            [sys.executable, "-m", "opencoord", "--smoke-frames", "3", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert ("could not open" in result.stderr) == warned


def test_live_and_scan_against_the_simulator(tmp_path: Path) -> None:
    import dearpygui.dearpygui as dpg

    from opencoord.core.settings import AppSettings
    from opencoord.device.scanner import Resolution
    from opencoord.device.simulator import SimulatedLink
    from opencoord.io.profile_store import ProfileStore
    from opencoord.ui.app import App
    from opencoord.ui.controller import Controller

    c = Controller(
        lambda _port: SimulatedLink(sweep_points=512, sweep_interval_s=0.02),
        port_lister=lambda: [],
        simulator=True,
    )
    (tmp_path / "cfg" / "profiles").mkdir(parents=True)
    (tmp_path / "cfg" / "profiles" / "broken.toml").write_text("[profile]\nname = 'x'\n")
    app = App(c, AppSettings(), profile_store=ProfileStore(tmp_path / "cfg"))
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
        mid = c.add_marker(scan.start_hz + 5 * MHZ)
        assert mid == 1
        assert c.freeze_reference() == "ref1"
        c.set_threshold_dbm(-80.0)
        c.auto_scale()
        c.set_trace_visible("avg", False)
        for _ in range(8):
            assert app.frame()
        assert dpg.get_item_configuration("spectrum.marker.0")["show"]
        assert dpg.get_value("spectrum.marker.0") == pytest.approx((scan.start_hz + 5 * MHZ) / 1e6)
        assert "M1" in dpg.get_item_configuration("spectrum.marker.0.note")["label"]
        assert dpg.get_item_configuration("spectrum.threshold")["show"]
        assert dpg.get_value("spectrum.threshold") == pytest.approx(-80.0)
        assert len(dpg.get_value("spectrum.trace.ref1")[0]) == len(scan.freqs_hz)
        assert not dpg.get_item_configuration("spectrum.trace.avg")["show"]
        assert dpg.get_item_configuration("markers.row.0")["show"]
        assert not dpg.get_item_configuration("markers.row.1")["show"]
        c.remove_marker(1)
        for _ in range(2):
            assert app.frame()
        assert not dpg.get_item_configuration("spectrum.marker.0")["show"]
        # Channel overlay, exclusion zone and the analysis panel (scan view is 470-500 MHz).
        assert not dpg.get_item_configuration("overlay.channel.0")["show"]
        dpg.set_value("tabs", "tab.analysis")  # the panel only refreshes while it is visible
        c.set_overlay_enabled(True)
        c.add_exclusion_zone(480 * MHZ, 485 * MHZ)
        for _ in range(4):
            assert app.frame()
        assert dpg.get_item_configuration("overlay.channel.0")["show"]  # channel 21, 474 MHz
        assert dpg.get_item_configuration("overlay.channel.0")["label"] == "21"
        assert not dpg.get_item_configuration("overlay.channel.10")["show"]  # channel 31, outside
        assert dpg.get_item_configuration("overlay.span.0")["show"]
        assert dpg.get_item_configuration("overlay.zone.0")["show"]
        assert dpg.get_item_configuration("overlay.zone.0.note")["label"] == "X1"
        assert dpg.get_value("overlay.grid")[0][:2] == [470.0, 478.0]
        assert dpg.get_item_configuration("analysis.occ.row.0")["show"]
        assert dpg.get_item_configuration("analysis.zone.row.0")["show"]
        assert dpg.get_value("analysis.zone.start.0") == pytest.approx(480.0)
        assert not dpg.get_item_configuration("device.module")["show"]  # no expansion module
        c.set_overlay_enabled(False)
        for _ in range(2):
            assert app.frame()
        assert not dpg.get_item_configuration("overlay.channel.0")["show"]
        assert not dpg.get_item_configuration("overlay.span.0")["show"]
        assert dpg.get_item_configuration("overlay.zone.0")["show"]  # zones stay drawn
        for tab in ("markers", "analysis", "coordination", "profiles"):
            assert dpg.does_item_exist(f"tab.{tab}")
        # Item callbacks run on the UI thread (manual callback management).
        import threading

        seen: list[int] = []
        with dpg.window(tag="cb.test", show=False):
            dpg.add_button(tag="cb.button", callback=lambda *_: seen.append(threading.get_ident()))
        dpg.configure_item("cb.button", callback=lambda *_: seen.append(threading.get_ident()))
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_F24, callback=lambda *_: None)
        dpg.get_item_callback("cb.button")(None, None)  # direct call is trivially on this thread
        with dpg.item_handler_registry(tag="cb.handlers"):
            dpg.add_item_visible_handler(callback=lambda *_: seen.append(threading.get_ident()))
        dpg.bind_item_handler_registry("main", "cb.handlers")
        for _ in range(3):
            assert app.frame()
        assert len(seen) > 1 and set(seen) == {threading.get_ident()}
        # Record tab: recording, logger alert and replay of the file just written.
        dpg.set_value("tabs", "tab.record")
        rec = tmp_path / "smoke.ocrec"
        c.set_mode("live")
        c.set_range(560 * MHZ, 570 * MHZ)
        assert app.recording.start_recording(rec)
        c.start()
        deadline = time.monotonic() + 8
        while app.recording.recording_status().sweeps < 5:
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert app.frame()
        assert dpg.get_value("record.status").startswith("Recording to smoke.ocrec")
        assert dpg.get_item_configuration("record.button")["label"] == "Stop recording"
        app.recording.set_logger_threshold(-70.0)
        assert app.recording.enable_logger(tmp_path / "smoke.csv")
        while c.state.logger_alert is None:
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert app.frame()
        assert dpg.get_value("logger.alert").startswith("ALERT 563.3")
        assert dpg.get_value("logger.enable")
        assert dpg.get_item_configuration("logger.range.0")["show"]
        app.recording.disable_logger()
        assert app.recording.stop_recording()
        assert app.frame()
        assert dpg.get_value("record.status").startswith("Finishing")
        while app.recording.finishing:
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert rec.exists() and dpg.does_item_exist("replay.unfinished")
        assert not dpg.get_item_configuration("record.recover")["show"]
        c.stop()
        c.disconnect()
        while c.state.connection != "disconnected":
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert app.recording.open_replay(rec)
        while c.state.connection != "connected":
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        app.recording.set_replay_speed("Max")
        c.start()
        while c.state.running:
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        assert app.frame()
        assert c.state.message == "End of recording" and not c.state.retunable
        assert dpg.get_value("replay.position") == pytest.approx(1.0)
        assert "ended" in dpg.get_value("replay.info")
        assert dpg.get_item_configuration("replay.play")["label"] == "Play"
        # Profiles tab: load issue, template, widget edits, mode switch, save, delete confirm.
        dpg.set_value("tabs", "tab.profiles")
        pa = app.profiles

        def fire(tag: str, value: object = None) -> None:
            """Call a widget's callback like Dear PyGui would (as many arguments as it takes)."""
            callback = dpg.get_item_callback(tag)
            assert callback is not None
            n = len(inspect.signature(callback).parameters)
            callback(*(tag, value, dpg.get_item_user_data(tag))[:n])

        for _ in range(2):
            assert app.frame()
        assert not dpg.get_item_configuration("profiles.editor")["show"]
        assert not app.profiles_panel.is_typing()
        issue_texts = [
            dpg.get_value(i)
            for i in dpg.get_item_children("profiles.issues", 1)
            if dpg.get_value(i)
        ]
        assert any(t.startswith("broken.toml:") for t in issue_texts)
        pa.new_from_template("generic-analog-mic")
        for _ in range(2):
            assert app.frame()
        assert dpg.get_item_configuration("profiles.editor")["show"]
        assert dpg.get_value("profiles.name") == "Generic analog mic"
        assert "[not saved yet]" in dpg.get_value("profiles.header")
        assert dpg.get_value("profiles.range.0.stop") == pytest.approx(832.0)
        assert "361 candidate" in dpg.get_value("profiles.preview")
        assert dpg.get_item_configuration("profiles.save")["enabled"]
        assert dpg.get_value("profiles.override.im3_2tx.effective") == "100 kHz"
        # A widget edit goes through its callback into the draft; an invalid range shows red text.
        dpg.set_value("profiles.range.0.stop", 800.0)
        fire("profiles.range.0.stop", 800.0)
        assert app.frame()
        assert "start must be below stop" in dpg.get_value("profiles.errors")
        assert not dpg.get_item_configuration("profiles.save")["enabled"]
        dpg.set_value("profiles.range.0.stop", 830.0)
        fire("profiles.range.0.stop", 830.0)
        dpg.set_value("profiles.override.im3_2tx.check", True)
        fire("profiles.override.im3_2tx.check", True)
        assert app.frame()
        assert dpg.get_value("profiles.errors") == ""
        assert dpg.get_value("profiles.override.im3_2tx.effective") == "100 kHz (override)"
        assert dpg.get_item_configuration("profiles.override.im3_2tx.value")["enabled"]
        dpg.set_value("profiles.mode", "Groups (banks) of channels")
        fire("profiles.mode", "Groups (banks) of channels")
        for _ in range(2):
            assert app.frame()
        assert "Saving drops" in dpg.get_value("profiles.mode_warning")
        assert "Add at least one group" in dpg.get_value("profiles.errors")
        fire("profiles.group.add")
        for _ in range(2):
            assert app.frame()
        box = "profiles.group.0.channels"
        dpg.set_value(box, "470.125, 470.250\n471 junk")
        fire(box, "470.125, 470.250\n471 junk")
        assert app.frame()
        assert dpg.get_value(f"{box}.info") == "3 channels; 1 entry not understood"
        assert "'junk' is not a number" in dpg.get_value("profiles.errors")
        dpg.set_value(box, "470.125, 470.250\n471")
        fire(box, "470.125, 470.250\n471")
        assert app.frame()
        assert dpg.get_value("profiles.preview").startswith("3 candidate frequencies, 470.125")
        assert dpg.get_item_configuration("profiles.save")["enabled"]
        assert pa.save_profile()
        for _ in range(2):
            assert app.frame()
        assert (tmp_path / "cfg" / "profiles" / "generic-analog-mic.toml").exists()
        assert dpg.get_value("profiles.message") == "Saved profile 'Generic analog mic'"
        assert "unsaved" not in dpg.get_value("profiles.header")
        assert not dpg.get_item_configuration("profiles.save")["enabled"]
        fire("profiles.delete")
        for _ in range(2):
            assert app.frame()
        assert dpg.get_item_configuration("profiles.confirm")["show"]
        assert "Delete profile" in dpg.get_value("profiles.confirm.text")
        pa.confirm_pending()
        for _ in range(2):
            assert app.frame()
        assert not dpg.get_item_configuration("profiles.editor")["show"]
        assert not (tmp_path / "cfg" / "profiles" / "generic-analog-mic.toml").exists()
        # Spacing presets sub-tab.
        pa.select_preset("iem")
        for _ in range(2):
            assert app.frame()
        assert dpg.get_value("presets.name") == "iem"
        assert dpg.get_value("presets.value.carrier") == pytest.approx(600.0)
        assert dpg.get_item_configuration("presets.reset")["show"]
        assert dpg.get_value("presets.used_by") == "Not used by any profile"
        # Coordination tab: device row, locked carrier, Coordinate on the worker thread, results
        # table, plan lines and labels on the spectrum, check mode and the HTML export.
        dpg.set_value("tabs", "tab.coordination")
        ca = app.coordination
        fire("coord.device.add")
        assert app.frame()
        fire("coord.device.0.profile", "Generic analog mic")
        fire("coord.device.0.qty", 3)
        dpg.set_value("coord.lock.paste", "830.5")
        dpg.set_value("coord.lock.label", "Venue")
        fire("coord.lock.add")
        assert app.frame()
        assert [(r.profile, r.quantity) for r in ca.model.rows] == [("Generic analog mic", 3)]
        assert dpg.get_value("coord.lock.row.0.label") == "Venue"
        assert dpg.get_value("coord.devices.total") == "3 devices"
        fire("coord.run")
        assert app.frame()
        assert dpg.get_item_configuration("coord.cancel")["show"] or ca.result is not None
        while ca.running:
            assert time.monotonic() < deadline + 10, c.state.message
            assert app.frame()
        assert app.frame()
        assert ca.result is not None and ca.result.plan.stats.complete, ca.message
        assert dpg.get_value("coord.stats").startswith("Complete: all 3 devices")
        assert len(dpg.get_item_children("coord.results", 1)) == 3
        assert not dpg.get_item_configuration("coord.cancel")["show"]
        plan_x = dpg.get_value("spectrum.plan")[0]
        assert sorted(plan_x) == sorted(a.freq_hz / 1e6 for a in ca.result.plan.assignments)
        assert dpg.get_item_configuration("spectrum.plan")["show"]
        assert dpg.get_item_configuration("spectrum.plan.note.0")["label"] == (
            "Generic analog mic #1"
        )
        assert not dpg.get_item_configuration("spectrum.plan.note.3")["show"]
        fire("coord.show", False)
        assert app.frame()
        assert not dpg.get_item_configuration("spectrum.plan")["show"]
        fire("coord.show", True)
        fire("coord.edit")
        for _ in range(2):
            assert app.frame()
        assert dpg.get_value("coord.check.row.0").count(";") == 2
        fire("coord.check.run")
        while ca.running:
            assert time.monotonic() < deadline + 10, c.state.message
            assert app.frame()
        assert app.frame()
        assert dpg.get_value("coord.check.summary") == "No violations among 3 devices"
        dpg.set_value("coord.check.row.0", "825; 825.1")
        fire("coord.check.row.0", "825; 825.1")
        fire("coord.check.run")
        while ca.running:
            assert time.monotonic() < deadline + 10, c.state.message
            assert app.frame()
        assert app.frame()
        assert dpg.get_value("coord.check.summary").startswith("1 violation")
        assert len(dpg.get_item_children("coord.check.table", 1)) == 1
        html = tmp_path / "plan.html"
        app.file_ui.capture_plot(lambda rgba: ca.export("html", html, rgba))
        while not html.exists():
            assert time.monotonic() < deadline + 10, c.state.message
            assert app.frame()
        assert "data:image/png;base64," in html.read_text()
        # Sessions, export window, file dialog and the PNG plot capture.
        session = tmp_path / "smoke.opencoord"
        assert app.files.save(session)
        assert app.files.open(session)
        assert ca.result is not None and len(ca.model.locks) == 1
        app.file_ui.export_dialog()
        assert app.frame()
        assert dpg.get_item_configuration("files.export")["show"]
        dpg.configure_item("files.export", show=False)
        app.file_ui.open_session()
        for _ in range(3):
            assert app.frame()
        assert dpg.does_item_exist("files.dialog.1")
        assert dpg.get_viewport_title().startswith("smoke.opencoord")
        dpg.delete_item("files.dialog.1")
        shot = tmp_path / "plot.png"
        assert app.file_ui._export_to("png", "max", shot)
        deadline = time.monotonic() + 5
        while not shot.exists():
            assert time.monotonic() < deadline, c.state.message
            assert app.frame()
        data = shot.read_bytes()
        assert data.startswith(b"\x89PNG") and "Exported" in c.state.message
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
