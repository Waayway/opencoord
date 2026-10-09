"""Recording, replay and logger flows through the Controller (SimulatedLink, no display)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from opencoord.core.types import Sweep
from opencoord.device.link_api import Link
from opencoord.device.simulator import SimulatedLink
from opencoord.io import recording
from opencoord.io.recording import RecordingInfo, RecordingReader, RecordingWriter
from opencoord.ui.controller import Controller
from opencoord.ui.recording import RecordingActions

MHZ = 1_000_000
INFO = RecordingInfo("RF Explorer WSUB1G+", 10, None, "03.39", 50_000, 960 * MHZ, -30.0, -120.0)


def factory(_port: str | None) -> Link:
    return SimulatedLink(seed=1, sweep_interval_s=0.005)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def run_until(c: Controller, cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; state message: {c.state.message!r}")
        c.tick()
        time.sleep(0.003)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ctl() -> Iterator[Controller]:
    c = Controller(factory, port_lister=lambda: [], simulator=True)
    yield c
    c.shutdown()


@pytest.fixture
def actions(ctl: Controller, clock: FakeClock) -> RecordingActions:
    return RecordingActions(ctl, clock=clock, timestamp=lambda: "2026-10-09T10:00:00+00:00")


def connected(c: Controller) -> Controller:
    c.connect()
    run_until(c, lambda: c.state.connection == "connected")
    return c


def finish(c: Controller, actions: RecordingActions) -> None:
    run_until(c, lambda: not actions.finishing)


def write_recording(path: Path, count: int, *, points: int = 16, step_s: float = 0.001) -> Path:
    w = RecordingWriter(path, INFO)
    for i in range(count):
        freqs = 600 * MHZ + np.arange(points, dtype=np.float64) * 25_000
        dbm = np.full(points, -90.0 + i, dtype=np.float32)
        w.append(Sweep(freqs, dbm, 1000.0 + i * step_s))
    w.close()
    assert w.wait(10) and w.error is None
    return w.path


# --- recording ------------------------------------------------------------------------------------


def test_recording_needs_a_connection(actions: RecordingActions, tmp_path: Path) -> None:
    assert not actions.start_recording(tmp_path / "x.ocrec")
    assert "Connect" in actions.controller.state.message and not actions.recording


def test_record_live_sweeps_including_a_range_change(
    ctl: Controller, actions: RecordingActions, clock: FakeClock, tmp_path: Path
) -> None:
    connected(ctl)
    ctl.set_range(560 * MHZ, 570 * MHZ)
    ctl.start()
    assert actions.start_recording(tmp_path / "live")  # suffix is added
    run_until(ctl, lambda: actions.recording_status().sweeps >= 5)
    clock.now += 12.5
    status = actions.recording_status()
    assert status.active and status.elapsed_s == pytest.approx(12.5)
    assert status.path == tmp_path / "live.ocrec"
    ctl.set_range(600 * MHZ, 620 * MHZ)
    seen = actions.recording_status().sweeps
    run_until(ctl, lambda: actions.recording_status().sweeps >= seen + 5)
    assert actions.stop_recording()
    assert not actions.recording and actions.finishing
    assert "Finishing" in ctl.state.message
    finish(ctl, actions)
    assert "Saved recording" in ctl.state.message

    reader = RecordingReader.open(tmp_path / "live.ocrec")
    sweeps = list(reader.sweeps())
    assert len(sweeps) == reader.meta.sweep_count >= 10
    assert reader.meta.device is not None and reader.meta.device.model_code == 10
    ranges = {(s.start_hz, s.stop_hz) for s in sweeps}
    assert len(ranges) >= 2  # mixed axes (the retune)
    assert not (tmp_path / "live.ocrec.parts").exists()


def test_recording_is_finalised_on_shutdown(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    ctl.start()
    assert actions.start_recording(tmp_path / "s.ocrec")
    run_until(ctl, lambda: actions.recording_status().sweeps >= 3)
    ctl.shutdown()
    assert not actions.recording
    assert len(list(RecordingReader.open(tmp_path / "s.ocrec").sweeps())) >= 3


def test_second_recording_and_stop_without_recording(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    assert not actions.stop_recording()
    assert actions.start_recording(tmp_path / "a.ocrec")
    assert not actions.start_recording(tmp_path / "b.ocrec")
    assert not (tmp_path / "b.ocrec.parts").exists()


def test_unwritable_target_is_reported(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert not actions.start_recording(blocker / "sub" / "x.ocrec")
    assert "Cannot record" in ctl.state.message and not actions.recording


# --- replay ---------------------------------------------------------------------------------------


def test_replay_goes_through_the_normal_connect_path(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    path = write_recording(tmp_path / "r.ocrec", 30)
    assert actions.open_replay(path)
    assert ctl.state.connection == "connecting"
    run_until(ctl, lambda: ctl.state.connection == "connected")
    st = ctl.state
    assert not st.retunable and st.capabilities is not None
    assert st.capabilities.name == "RF Explorer WSUB1G+"
    status = actions.replay_status()
    assert status is not None and status.name == "r.ocrec" and status.paused
    assert status.position == 0.0 and status.total_sweeps == 30

    actions.set_replay_speed("Max")
    ctl.start()
    assert st.running
    run_until(ctl, lambda: not st.running)
    assert st.message == "End of recording"
    assert st.connection == "connected"  # the traces stay on screen
    assert st.waterfall.pushes == 30
    assert st.traces.live is not None and st.traces.max_hold is not None
    assert st.traces.max_hold.dbm.max() == pytest.approx(-90.0 + 29)
    status = actions.replay_status()
    assert status is not None and status.ended and status.position == 1.0

    ctl.start()  # starting again after the end plays from the beginning
    run_until(ctl, lambda: not st.running)
    assert st.waterfall.pushes == 60


def test_replay_pause_and_resume(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    path = write_recording(tmp_path / "p.ocrec", 50, step_s=0.02)
    actions.open_replay(path)
    run_until(ctl, lambda: ctl.state.connection == "connected")
    st = ctl.state
    actions.set_replay_speed("4x")
    actions.toggle_replay_pause()  # not running yet: this starts the replay
    assert st.running
    run_until(ctl, lambda: st.waterfall.pushes >= 3)
    actions.toggle_replay_pause()
    status = actions.replay_status()
    assert status is not None and status.paused and st.running
    for _ in range(5):  # let in-flight sweeps arrive, then check that none follow
        ctl.tick()
        time.sleep(0.01)
    frozen = st.waterfall.pushes
    for _ in range(20):
        ctl.tick()
        time.sleep(0.005)
    assert st.waterfall.pushes == frozen
    actions.toggle_replay_pause()
    run_until(ctl, lambda: st.waterfall.pushes > frozen)
    ctl.stop()
    status = actions.replay_status()
    assert status is not None and status.paused  # stopping pauses the replay


def test_scan_mode_is_not_available_on_a_replay(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    ctl.set_mode("scan")
    actions.open_replay(write_recording(tmp_path / "m.ocrec", 3))
    run_until(ctl, lambda: ctl.state.connection == "connected")
    assert ctl.state.mode == "live"  # a saved scan mode is dropped on connect
    ctl.set_mode("scan")
    assert ctl.state.mode == "live" and "Scan mode" in ctl.state.message
    ctl.disconnect()
    run_until(ctl, lambda: ctl.state.connection == "disconnected")
    assert ctl.state.retunable
    ctl.set_mode("scan")
    assert ctl.state.mode == "scan"


def test_open_replay_refused_while_connected_and_bad_files(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    assert not actions.open_replay(tmp_path / "x.ocrec")
    assert "Disconnect" in ctl.state.message
    ctl.disconnect()
    run_until(ctl, lambda: ctl.state.connection == "disconnected")
    bad = tmp_path / "bad.ocrec"
    bad.write_bytes(b"garbage")
    actions.open_replay(bad)
    run_until(ctl, lambda: ctl.state.connection == "disconnected" and ctl.state.error is not None)
    assert "bad.ocrec" in (ctl.state.error or "")


def test_replay_can_be_recorded_again_unchanged(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    source = write_recording(tmp_path / "src.ocrec", 20)
    actions.open_replay(source)
    run_until(ctl, lambda: ctl.state.connection == "connected")
    actions.set_replay_speed("Max")
    assert actions.start_recording(tmp_path / "copy.ocrec")
    ctl.start()
    run_until(ctl, lambda: not ctl.state.running)
    assert actions.stop_recording()
    finish(ctl, actions)
    a = list(RecordingReader.open(source).sweeps())
    b = list(RecordingReader.open(tmp_path / "copy.ocrec").sweeps())
    assert len(a) == len(b) == 20
    assert all(
        x.timestamp == y.timestamp
        and np.array_equal(x.freqs_hz, y.freqs_hz)
        and np.array_equal(x.dbm, y.dbm)
        for x, y in zip(a, b, strict=True)
    )


def test_replay_status_is_none_for_a_device(ctl: Controller, actions: RecordingActions) -> None:
    assert actions.replay_status() is None
    connected(ctl)
    assert actions.replay_status() is None and ctl.replay_link is None
    actions.set_replay_speed("4x")  # harmless
    actions.toggle_replay_pause()
    actions.restart_replay()


# --- logger ---------------------------------------------------------------------------------------


def lines(path: Path) -> list[str]:
    return path.read_text("utf-8").splitlines()


def test_logger_rows_alert_and_debounce(
    ctl: Controller, actions: RecordingActions, clock: FakeClock, tmp_path: Path
) -> None:
    connected(ctl)
    ctl.set_range(560 * MHZ, 570 * MHZ)  # holds the simulated 563.3 MHz carrier (-48 dBm)
    ctl.start()
    run_until(ctl, lambda: ctl.state.traces.live is not None)
    actions.set_logger_threshold(-70.0)
    assert actions.add_logger_range(561 * MHZ, 565 * MHZ)
    assert actions.add_logger_range(567 * MHZ, 569 * MHZ)  # nothing but noise here
    log_path = tmp_path / "log.csv"
    assert actions.enable_logger(log_path)
    assert actions.logger_enabled and not actions.add_logger_range(1, 2)
    status = actions.logger_status()
    assert status.enabled and status.next_row_in_s == pytest.approx(60.0)

    run_until(ctl, lambda: ctl.state.logger_alert is not None)
    st = ctl.state
    assert st.logger_alert is not None and st.logger_alert.startswith("ALERT 563.3")
    assert st.message.startswith("ALERT")
    for _ in range(30):  # more sweeps within the interval: still one alert, no extra lines
        ctl.tick()
        time.sleep(0.003)
    assert actions.logger_status().alerts == 1
    body = lines(log_path)
    assert body[0] == "timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz,kind"
    alert_lines = [x for x in body if x.endswith(",ALERT")]
    assert len(alert_lines) == 1 and alert_lines[0].startswith(
        "2026-10-09T10:00:00+00:00,561.000000,565.000000,"
    )
    assert len(body) == 2  # header + alert, no row yet

    clock.now += 61
    ctl.tick()
    rows = [x for x in lines(log_path) if x.endswith(",DATA")]
    assert len(rows) == 2
    first, second = (r.split(",") for r in rows)
    assert first[1:3] == ["561.000000", "565.000000"] and float(first[3]) > -60
    assert first[4] == "563.300000" or abs(float(first[4]) - 563.3) < 0.2
    assert float(second[3]) < -90  # noise only
    assert actions.logger_status().rows_written == 2

    clock.now += 61  # the alert may fire again after an interval
    run_until(ctl, lambda: actions.logger_status().alerts == 2)
    ctl.set_range(600 * MHZ, 610 * MHZ)  # the logger keeps running through a retune


def test_logger_defaults_to_the_view_range_and_follows_the_threshold_line(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    ctl.set_range(560 * MHZ, 570 * MHZ)
    ctl.start()
    run_until(ctl, lambda: ctl.state.traces.live is not None)
    ctl.set_threshold_dbm(-70.0)
    assert actions.effective_threshold() == -70.0
    assert actions.enable_logger(tmp_path / "d.csv")
    assert [(r.start_hz, r.stop_hz) for r in actions.logger_ranges] == [ctl.state.view_range_hz]
    run_until(ctl, lambda: ctl.state.logger_alert is not None)
    actions.clear_alert()
    assert ctl.state.logger_alert is None
    actions.set_logger_threshold(-10.0)  # own threshold wins; nothing exceeds it
    assert actions.effective_threshold() == -10.0
    actions.set_logger_threshold(None)
    assert actions.effective_threshold() == -70.0


def test_disable_logger_writes_the_partial_interval(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    ctl.start()
    assert actions.enable_logger(tmp_path / "e.csv")
    run_until(ctl, lambda: ctl.state.traces.live is not None)
    ctl.tick()
    actions.disable_logger()
    assert not actions.logger_enabled
    body = lines(tmp_path / "e.csv")
    assert len(body) >= 2 and body[1].endswith(",DATA")
    # Ranges are editable again, and the interval is validated.
    assert actions.add_logger_range(500 * MHZ, 510 * MHZ)
    assert not actions.set_logger_interval(0.2) and actions.set_logger_interval(30)
    actions.remove_logger_range(0)
    actions.remove_logger_range(99)


def test_logger_file_error_is_reported(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert not actions.enable_logger(blocker / "log.csv")
    assert "Cannot start the logger" in ctl.state.message and not actions.logger_enabled


def test_logger_appends_to_an_existing_file(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    path = tmp_path / "again.csv"
    for _ in range(2):
        assert actions.enable_logger(path)
        actions.disable_logger()
    header = "timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz,kind"
    assert lines(path).count(header) == 1


# --- recovery, unfinished recordings, replay details ---------------------------------------------


def stale_parts(path: Path, count: int = 256) -> Path:
    """Leave an unfinished recording behind, as a crash would."""
    w = RecordingWriter(path, INFO)
    for i in range(count):
        w.append(
            Sweep(600 * MHZ + np.arange(8) * 25_000.0, np.full(8, -90.0, np.float32), float(i))
        )
    assert w.wait_idle()
    return path.with_name(path.name + ".parts")


def test_a_crashed_recording_can_be_recovered_from_the_app(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    target = tmp_path / "crash.ocrec"
    parts = stale_parts(target)
    assert not actions.start_recording(target)
    assert actions.pending_recovery == parts and "Recover" in ctl.state.message
    assert not actions.recording and parts.exists()
    assert actions.recover() and actions.finishing
    assert not actions.start_recording(target)  # still working on it
    finish(ctl, actions)
    assert actions.pending_recovery is None and "Recovered 256 sweeps" in ctl.state.message
    assert target.exists() and not parts.exists()
    assert len(list(RecordingReader.open(target).sweeps())) == 256
    ctl.start()
    assert not actions.start_recording(target)  # the recovered file is never replaced
    assert "already exists" in ctl.state.message
    assert actions.start_recording(tmp_path / "again.ocrec")
    assert actions.stop_recording()
    finish(ctl, actions)


def test_unfinished_recording_folder_replays(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    parts = stale_parts(tmp_path / "u.ocrec")
    assert actions.open_replay(parts)
    run_until(ctl, lambda: ctl.state.connection == "connected")
    status = actions.replay_status()
    assert status is not None and status.total_sweeps == 256


def test_stop_and_start_does_not_skip_recorded_sweeps(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    actions.open_replay(write_recording(tmp_path / "k.ocrec", 40))
    run_until(ctl, lambda: ctl.state.connection == "connected")
    actions.set_replay_speed("Max")
    ctl.start()
    link = ctl.replay_link
    assert link is not None
    deadline = time.monotonic() + 5
    while link.sweeps.qsize() < 40:  # the worker queued everything; nothing consumed yet
        assert time.monotonic() < deadline
        time.sleep(0.003)
    ctl.stop()
    for _ in range(5):
        ctl.tick()  # a stopped replay keeps its queued sweeps
    assert ctl.state.waterfall.pushes == 0
    ctl.start()
    run_until(ctl, lambda: not ctl.state.running)
    assert ctl.state.waterfall.pushes == 40


def test_logger_uses_recorded_time_during_a_replay(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    actions.open_replay(write_recording(tmp_path / "t.ocrec", 30))
    run_until(ctl, lambda: ctl.state.connection == "connected")
    actions.set_replay_speed("Max")
    actions.add_logger_range(600 * MHZ, 601 * MHZ)
    actions.set_logger_threshold(-85.0)  # the recorded level rises from -90 by 1 dB per sweep
    log_path = tmp_path / "replay.csv"
    assert actions.enable_logger(log_path)
    ctl.start()
    run_until(ctl, lambda: not ctl.state.running)
    actions.disable_logger()
    alerts = [x for x in lines(log_path) if x.endswith(",ALERT")]
    assert len(alerts) == 1
    assert alerts[0].startswith("1970-01-01T00:16:40+00:00,")  # sweep time 1000.006 s, not now
    assert ctl.state.logger_alert is not None


def test_logger_redirects_from_another_layout(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    old = tmp_path / "old.csv"
    old.write_text("timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz\n")
    assert actions.enable_logger(old)
    assert "another layout" in ctl.state.message and "old-1.csv" in ctl.state.message
    assert actions.logger_status().path == tmp_path / "old-1.csv"
    actions.disable_logger()
    assert old.read_text().count("\n") == 1


def test_a_finishing_writer_is_driven_by_ticks_alone(
    ctl: Controller, actions: RecordingActions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer is behind when stopped: leftover chunks and the finish request must still be
    handed over by the controller's tick (no wait()/poll() by the test)."""
    gate = threading.Event()
    real = recording.write_atomic

    def slow(path: Path, data: bytes) -> None:
        if path.name.startswith("chunk"):
            gate.wait(10)
        real(path, data)

    monkeypatch.setattr(recording, "write_atomic", slow)
    connected(ctl)
    assert actions.start_recording(tmp_path / "behind.ocrec")
    writer = actions._writer
    assert writer is not None
    total = recording.CHUNK_SWEEPS * (recording.MAX_QUEUED_CHUNKS + 4)
    for i in range(total):
        writer.append(Sweep(600 * MHZ + np.arange(8) * 25_000.0, np.full(8, -90.0, np.float32), i))
    assert actions.stop_recording()
    gate.set()
    run_until(ctl, lambda: not actions.finishing, timeout=10)
    assert "Saved recording" in ctl.state.message
    assert len(list(RecordingReader.open(tmp_path / "behind.ocrec").sweeps())) == total


def test_logger_restarts_when_the_replay_is_rewound(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    actions.open_replay(write_recording(tmp_path / "rw.ocrec", 30))
    run_until(ctl, lambda: ctl.state.connection == "connected")
    actions.set_replay_speed("Max")
    actions.add_logger_range(600 * MHZ, 601 * MHZ)
    actions.set_logger_threshold(-85.0)
    assert actions.enable_logger(tmp_path / "rw.csv")
    ctl.start()
    run_until(ctl, lambda: not ctl.state.running)
    assert actions._engine is not None and actions._alerts == 1
    actions.restart_replay()
    ctl.start()
    run_until(ctl, lambda: not ctl.state.running)
    assert actions._alerts == 2  # the second pass alerts again: time went back, debounce reset


def test_shutdown_waits_for_a_recovery_in_progress(
    ctl: Controller, actions: RecordingActions, tmp_path: Path
) -> None:
    connected(ctl)
    target = tmp_path / "sd.ocrec"
    stale_parts(target)
    assert not actions.start_recording(target)
    assert actions.recover()
    ctl.shutdown()
    assert target.exists() and not actions.finishing
