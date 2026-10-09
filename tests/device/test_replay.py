"""ReplayLink and its scheduler: timing is driven by a fake clock, no sleeping, no threads."""

from __future__ import annotations

import math
import queue
import time
from pathlib import Path

import numpy as np
import pytest

from opencoord.core.types import Sweep
from opencoord.device import replay
from opencoord.device.link_api import LinkEvent
from opencoord.device.replay import ReplayLink, ReplayScheduler
from opencoord.io.recording import RecordingInfo, RecordingWriter

INFO = RecordingInfo("RF Explorer WSUB1G+", 10, None, "03.39", 50_000, 960_000_000, -30.0, -120.0)


def sweep(t: float, start: int = 470_000_000, step: int = 25_000, points: int = 8) -> Sweep:
    freqs = start + np.arange(points, dtype=np.float64) * step
    return Sweep(freqs, np.full(points, -90.0, dtype=np.float32), t)


def timestamps(sweeps: list[Sweep]) -> list[float]:
    return [s.timestamp for s in sweeps]


def scheduler(times: list[float], total: int | None = None) -> ReplayScheduler:
    return ReplayScheduler(
        iter([sweep(t) for t in times]), total if total is not None else len(times)
    )


# --- scheduler ------------------------------------------------------------------------------------


def test_scheduler_starts_paused_and_emits_first_sweep_immediately() -> None:
    s = scheduler([100.0, 101.0, 102.0])
    assert s.paused and s.due(5.0) == [] and s.wait_s(5.0) is None
    s.set_paused(False, 10.0)
    assert timestamps(s.due(10.0)) == [100.0]
    assert s.wait_s(10.0) == pytest.approx(1.0)


def test_scheduler_honours_original_timing_at_1x() -> None:
    s = scheduler([100.0, 100.5, 102.0, 102.1])
    s.set_paused(False, 0.0)
    assert timestamps(s.due(0.0)) == [100.0]
    assert s.due(0.49) == []
    assert timestamps(s.due(0.5)) == [100.5]
    assert s.wait_s(0.5) == pytest.approx(1.5)
    assert s.due(1.9) == []
    assert timestamps(s.due(2.0)) == [102.0]
    assert timestamps(s.due(2.1)) == [102.1]
    assert s.finished and s.emitted == 4 and s.wait_s(2.1) is None


def test_scheduler_scales_timing_by_speed() -> None:
    s = scheduler([0.0, 1.0, 2.0, 3.0])
    s.set_speed(4.0, 0.0)
    s.set_paused(False, 0.0)
    assert timestamps(s.due(0.0)) == [0.0]
    assert s.due(0.2) == []
    assert timestamps(s.due(0.25)) == [1.0]
    assert timestamps(s.due(0.75)) == [2.0, 3.0]


def test_scheduler_max_speed_emits_everything_up_to_the_limit() -> None:
    s = scheduler([float(i) for i in range(10)])
    s.set_speed(math.inf, 0.0)
    s.set_paused(False, 0.0)
    assert len(s.due(0.0, limit=4)) == 4
    assert s.wait_s(0.0) == 0.0
    assert len(s.due(0.0, limit=100)) == 6 and s.finished


def test_scheduler_pause_keeps_the_remaining_wait() -> None:
    s = scheduler([0.0, 2.0, 4.0])  # gaps 2 s
    s.set_paused(False, 0.0)
    s.due(0.0)
    s.set_paused(True, 0.5)  # 1.5 s still to wait
    assert s.due(100.0) == [] and s.wait_s(100.0) is None
    s.set_paused(False, 100.0)
    assert s.wait_s(100.0) == pytest.approx(1.5)
    assert s.due(101.4) == []
    assert timestamps(s.due(101.5)) == [2.0]


def test_scheduler_speed_change_rescales_the_remaining_wait() -> None:
    s = scheduler([0.0, 2.0, 4.0])
    s.set_paused(False, 0.0)
    s.due(0.0)  # next in 2 s
    s.set_speed(4.0, 1.0)  # 1 s of wait left at 1x -> 0.25 s at 4x
    assert s.wait_s(1.0) == pytest.approx(0.25)
    s.set_speed(1.0, 1.0)
    assert s.wait_s(1.0) == pytest.approx(1.0)
    s.set_speed(math.inf, 1.0)
    assert s.wait_s(1.0) == 0.0
    s.set_speed(1.0, 1.0)  # leaving max speed: do not wait the original gap forever
    assert s.wait_s(1.0) == 0.0


def test_scheduler_caps_long_gaps_and_does_not_burst_after_a_stall() -> None:
    s = scheduler([0.0, 3600.0, 3601.0, 3602.0, 3603.0])
    s.set_paused(False, 0.0)
    s.due(0.0)
    assert s.wait_s(0.0) == pytest.approx(replay.MAX_GAP_S)
    assert timestamps(s.due(replay.MAX_GAP_S)) == [3600.0]
    # The UI froze for a minute: only what is due is emitted, then the schedule restarts from now.
    out = s.due(60.0)
    assert timestamps(out) == [3601.0, 3602.0]
    assert s.wait_s(60.0) == pytest.approx(1.0)


def test_scheduler_handles_equal_and_backwards_timestamps() -> None:
    s = scheduler([5.0, 5.0, 4.0, math.nan, 6.0])
    s.set_paused(False, 0.0)
    assert len(s.due(0.0, limit=10)) == 5


def test_scheduler_progress_and_position() -> None:
    s = scheduler([0.0, 0.0, 0.0, 0.0], total=4)
    assert s.position == 0.0
    s.set_paused(False, 0.0)
    s.due(0.0, limit=2)
    assert s.position == pytest.approx(0.5)
    s.due(0.0)
    assert s.position == 1.0
    assert ReplayScheduler(iter([]), 0).position == 1.0


# --- link ---------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def make_recording(tmp_path: Path, sweeps: list[Sweep], info: RecordingInfo | None = INFO) -> Path:
    w = RecordingWriter(tmp_path / "r.ocrec", info)
    for s in sweeps:
        w.append(s)
    w.close()
    assert w.wait(10) and w.error is None
    return w.path


def drain(q: queue.Queue[Sweep]) -> list[Sweep]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def events(link: ReplayLink) -> list[LinkEvent]:
    out = []
    while True:
        try:
            out.append(link.events.get_nowait())
        except queue.Empty:
            return out


def test_open_reports_model_config_and_connected(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(1.0), sweep(2.0)])
    link = ReplayLink(path, clock=FakeClock(), threaded=False)
    assert not link.is_open and link.model is None and link.config is None
    link.open()
    assert link.is_open and not link.retunable
    assert link.model is not None and link.model.main_code == 10 and link.model.firmware == "03.39"
    assert link.capabilities is not None and link.capabilities.name == "RF Explorer WSUB1G+"
    cfg = link.config
    assert cfg is not None
    assert (cfg.start_hz, cfg.step_hz, cfg.sweep_points) == (470_000_000, 25_000, 8)
    assert cfg.stop_hz == 470_175_000
    (ev,) = events(link)
    assert ev.kind == "connected" and "r.ocrec" in ev.message
    link.open()  # no-op
    assert events(link) == []
    link.close()
    assert not link.is_open and events(link)[-1].kind == "disconnected"


def test_link_is_paused_until_told_and_follows_the_recorded_timing(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(10.0), sweep(10.5), sweep(11.5)])
    clock = FakeClock()
    link = ReplayLink(path, clock=clock, threaded=False)
    link.open()
    events(link)
    link.pump()
    assert drain(link.sweeps) == [] and link.position == 0.0
    link.set_paused(False)
    link.pump()
    assert timestamps(drain(link.sweeps)) == [10.0]
    clock.now += 0.4
    link.pump()
    assert drain(link.sweeps) == []
    clock.now += 0.1
    link.pump()
    assert timestamps(drain(link.sweeps)) == [10.5]
    assert link.position == pytest.approx(2 / 3)
    clock.now += 1.0
    link.pump()
    assert timestamps(drain(link.sweeps)) == [11.5]
    ev = events(link)
    assert [(e.kind, e.message) for e in ev] == [("disconnected", "End of recording")]
    assert link.ended and link.is_open and link.position == 1.0
    link.pump()
    assert events(link) == []  # the end is reported once


def test_speed_and_pause(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(float(i)) for i in range(5)])
    clock = FakeClock()
    link = ReplayLink(path, clock=clock, threaded=False)
    link.open()
    link.set_speed(4.0)
    link.set_paused(False)
    link.pump()
    clock.now += 0.25
    link.pump()
    assert timestamps(drain(link.sweeps)) == [0.0, 1.0]
    link.set_paused(True)
    clock.now += 60
    link.pump()
    assert drain(link.sweeps) == []
    link.set_paused(False)
    link.set_speed(math.inf)
    link.pump()
    assert timestamps(drain(link.sweeps)) == [2.0, 3.0, 4.0]
    assert link.ended


def test_config_follows_each_sweeps_axis(tmp_path: Path) -> None:
    a, b = sweep(0.0), sweep(0.1, start=600_000_000, step=50_000, points=20)
    link = ReplayLink(make_recording(tmp_path, [a, a, b]), clock=FakeClock(), threaded=False)
    link.open()
    first = link.config
    link.set_speed(math.inf)
    link.set_paused(False)
    link.pump()
    assert len(drain(link.sweeps)) == 3
    cfg = link.config
    assert cfg is not None and first is not None and cfg is not first
    assert (cfg.start_hz, cfg.step_hz, cfg.sweep_points) == (600_000_000, 50_000, 20)
    assert link.capabilities is not None and link.capabilities.max_hz == 960_000_000


def test_full_queue_applies_backpressure_instead_of_dropping(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(float(i)) for i in range(10)])
    link = ReplayLink(path, clock=FakeClock(), threaded=False, queue_size=4)
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    got: list[float] = []
    for _ in range(10):
        link.pump()
        assert link.sweeps.qsize() <= 4
        got += timestamps(drain(link.sweeps))
    assert got == [float(i) for i in range(10)]
    assert link.ended


def test_retuning_is_not_supported(tmp_path: Path) -> None:
    link = ReplayLink(make_recording(tmp_path, [sweep(0.0)]), clock=FakeClock(), threaded=False)
    link.open()
    for call in (
        lambda: link.set_span(1, 2),
        lambda: link.hold(),
        lambda: link.switch_module(True),
        lambda: link.set_sweep_points(112),
    ):
        with pytest.raises(NotImplementedError):
            call()


def test_rewind_restarts_from_the_beginning(tmp_path: Path) -> None:
    link = ReplayLink(
        make_recording(tmp_path, [sweep(0.0), sweep(1.0)]), clock=FakeClock(), threaded=False
    )
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    link.pump()
    assert link.ended and len(drain(link.sweeps)) == 2
    link.rewind()
    assert not link.ended and link.position == 0.0
    link.set_paused(False)
    link.pump()
    assert timestamps(drain(link.sweeps)) == [0.0, 1.0] and link.ended


def test_open_errors(tmp_path: Path) -> None:
    link = ReplayLink(tmp_path / "missing.ocrec", clock=FakeClock(), threaded=False)
    with pytest.raises(ConnectionError, match="not found"):
        link.open()
    assert events(link)[-1].kind == "error"
    empty = make_recording(tmp_path, [])
    with pytest.raises(ConnectionError, match="no sweeps"):
        ReplayLink(empty, clock=FakeClock(), threaded=False).open()
    junk = tmp_path / "junk.ocrec"
    junk.write_bytes(b"nope")
    with pytest.raises(ConnectionError, match=r"junk\.ocrec"):
        ReplayLink(junk, clock=FakeClock(), threaded=False).open()


def test_recording_without_device_info_still_opens(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(0.0)], info=None)
    link = ReplayLink(path, clock=FakeClock(), threaded=False)
    link.open()
    assert link.model is not None and link.capabilities is not None and link.config is not None
    assert link.capabilities.min_hz <= link.config.start_hz


def test_damaged_chunk_midway_reports_an_error_and_ends(tmp_path: Path) -> None:
    sweeps = [sweep(i * 0.01) for i in range(300)]
    w = RecordingWriter(tmp_path / "d.ocrec", INFO)
    for s in sweeps:
        w.append(s)
    parts_chunk = tmp_path / "d.ocrec.parts"
    w.flush()
    assert w.wait_idle()
    (parts_chunk / "chunk_000001.npz").write_bytes(b"damaged")  # second chunk
    link = ReplayLink(parts_chunk, clock=FakeClock(), threaded=False)
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    for _ in range(8):
        link.pump()
    kinds = [e.kind for e in events(link)]
    assert "error" in kinds and kinds[-1] == "disconnected"
    assert link.ended
    assert 0 < len(drain(link.sweeps)) <= 256 + 64


def test_threaded_replay_at_max_speed_delivers_everything(tmp_path: Path) -> None:
    path = make_recording(tmp_path, [sweep(float(i)) for i in range(40)])
    link = ReplayLink(path, queue_size=8)
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    got: list[float] = []
    deadline = time.monotonic() + 5
    while not link.ended or not link.sweeps.empty():
        assert time.monotonic() < deadline
        got += timestamps(drain(link.sweeps))
        time.sleep(0.002)
    got += timestamps(drain(link.sweeps))
    assert got == [float(i) for i in range(40)]
    assert events(link)[-1].message == "End of recording"
    link.close()
    assert not link.is_open


def test_rewind_drops_queued_sweeps_of_the_old_run(tmp_path: Path) -> None:
    link = ReplayLink(
        make_recording(tmp_path, [sweep(float(i)) for i in range(6)]),
        clock=FakeClock(),
        threaded=False,
    )
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    link.pump()
    assert link.sweeps.qsize() == 6  # not consumed
    link.rewind()
    assert link.sweeps.qsize() == 0
    link.set_paused(False)
    link.pump()
    assert timestamps(drain(link.sweeps)) == [float(i) for i in range(6)]


def test_chunk_decoding_happens_outside_the_lock(tmp_path: Path) -> None:
    link = ReplayLink(
        make_recording(tmp_path, [sweep(float(i)) for i in range(600)]),
        clock=FakeClock(),
        threaded=False,
        queue_size=1000,
    )
    link.open()
    link.set_speed(math.inf)
    link.set_paused(False)
    seen: list[bool] = []
    real = link._feed.prefetch  # type: ignore[union-attr]

    def spy() -> None:
        # RLock: _is_owned() is true only while *this* thread holds it.
        seen.append(link._lock._is_owned())  # type: ignore[attr-defined]
        real()

    link._feed.prefetch = spy  # type: ignore[union-attr,method-assign]
    link.pump()
    assert seen and not any(seen)
    assert len(drain(link.sweeps)) == 64
