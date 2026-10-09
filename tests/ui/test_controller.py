"""Controller: intents and tick() against the simulator, without Dear PyGui."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from opencoord.core.settings import AppSettings
from opencoord.core.types import Sweep
from opencoord.device.link import SerialPort
from opencoord.device.link_api import Link, LinkEvent
from opencoord.device.scanner import Resolution
from opencoord.device.simulator import SimulatedLink
from opencoord.ui.controller import Controller

MHZ = 1_000_000


class Factory:
    """Link factory recording the ports asked for; builds simulators by default."""

    def __init__(self, make: Callable[[], Link] | None = None) -> None:
        self.ports: list[str | None] = []
        self.links: list[Link] = []
        self._make = make or (lambda: SimulatedLink(seed=1, sweep_interval_s=0.01))

    def __call__(self, port: str | None) -> Link:
        self.ports.append(port)
        link = self._make()
        self.links.append(link)
        return link


class FailingLink(SimulatedLink):
    def open(self, timeout_s: float = 5.0) -> None:
        raise ConnectionError("Permission denied on /dev/ttyUSB0")


class ExclusivePort:
    """A serial port that, like ``exclusive=True``, cannot be opened twice."""

    def __init__(self) -> None:
        self.held = False
        self.opens = 0


class ExclusiveLink(SimulatedLink):
    """Holds ``port`` from open() until close() finishes; close() is slow like SerialLink's join."""

    def __init__(self, port: ExclusivePort, *, close_s: float = 0.3, connect_delay_s: float = 0.0):
        super().__init__(sweep_interval_s=0.01, connect_delay_s=connect_delay_s)
        self._port = port
        self._close_s = close_s

    def open(self, timeout_s: float = 5.0) -> None:
        if self._port.held:
            raise ConnectionError("Port busy")
        self._port.held = True
        self._port.opens += 1
        super().open(timeout_s)

    def close(self) -> None:
        was_open = self.is_open
        super().close()
        if was_open:
            time.sleep(self._close_s)
            self._port.held = False


class PortLink(SimulatedLink):
    """A simulator that reports the port it found, like SerialLink(port=None)."""

    active_port = ("/dev/ttyUSB3", 500_000)


def run_until(c: Controller, cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; state message: {c.state.message!r}")
        c.tick()
        time.sleep(0.005)


@pytest.fixture
def factory() -> Factory:
    return Factory()


@pytest.fixture
def ctl(factory: Factory) -> Iterator[Controller]:
    c = Controller(factory, port_lister=lambda: [], simulator=True)
    yield c
    c.shutdown()


def connected(c: Controller) -> Controller:
    c.connect()
    run_until(c, lambda: c.state.connection == "connected")
    return c


# --- startup / connecting ---------------------------------------------------------------------


def test_startup_does_not_probe_ports_by_default(factory: Factory) -> None:
    ports = [SerialPort("/dev/ttyUSB0", "CP2102N", True)]
    c = Controller(
        factory, settings=AppSettings(last_port="/dev/ttyUSB0"), port_lister=lambda: ports
    )
    c.startup()
    assert factory.ports == []
    assert c.state.ports == ports
    assert c.state.connection == "disconnected"


def test_startup_auto_connects_to_the_last_port_when_enabled(factory: Factory) -> None:
    s = AppSettings(last_port="/dev/ttyUSB0", auto_connect=True)
    c = Controller(factory, settings=s, port_lister=lambda: [])
    c.startup()
    assert factory.ports == ["/dev/ttyUSB0"]
    c.shutdown()


def test_auto_connect_without_a_last_port_does_nothing(factory: Factory) -> None:
    c = Controller(factory, settings=AppSettings(auto_connect=True), port_lister=lambda: [])
    c.startup()
    assert factory.ports == []


def test_simulator_connects_on_startup(factory: Factory) -> None:
    c = Controller(factory, port_lister=lambda: [], simulator=True)
    c.startup()
    run_until(c, lambda: c.state.connection == "connected")
    c.shutdown()


def test_connect_opens_the_link_off_the_ui_thread() -> None:
    factory = Factory(lambda: SimulatedLink(connect_delay_s=0.3))
    c = Controller(factory, port_lister=lambda: [], simulator=True)
    t0 = time.monotonic()
    c.connect()
    assert time.monotonic() - t0 < 0.1
    assert c.state.connection == "connecting"
    run_until(c, lambda: c.state.connection == "connected")
    assert c.state.capabilities is not None and c.state.model is not None
    assert c.state.config is not None
    assert c.state.error is None
    c.shutdown()


def test_connect_failure_is_reported() -> None:
    c = Controller(Factory(FailingLink), port_lister=lambda: [])
    c.connect("/dev/ttyUSB0")
    run_until(c, lambda: c.state.connection == "disconnected")
    assert c.state.error == "Permission denied on /dev/ttyUSB0"
    assert "Permission denied" in c.state.message


def test_successful_connect_remembers_the_port() -> None:
    c = Controller(Factory(PortLink), port_lister=lambda: [])
    connected(c)
    assert c.current_settings(AppSettings()).last_port == "/dev/ttyUSB3"
    c.shutdown()


def test_simulator_never_becomes_the_last_port(ctl: Controller) -> None:
    connected(ctl)
    base = AppSettings(last_port="/dev/ttyUSB0")
    assert ctl.current_settings(base).last_port == "/dev/ttyUSB0"


def test_disconnect(ctl: Controller, factory: Factory) -> None:
    connected(ctl)
    ctl.disconnect()
    assert ctl.state.connection == "disconnecting"
    assert ctl.state.capabilities is None
    link = factory.links[0]
    run_until(ctl, lambda: ctl.state.connection == "disconnected")
    assert not link.is_open
    assert ctl.state.message == "Disconnected"


def test_link_events_drive_the_connection_state(ctl: Controller, factory: Factory) -> None:
    connected(ctl)
    events: queue.Queue[LinkEvent] = factory.links[0].events
    events.put(LinkEvent("disconnected", "RF Explorer lost; reconnecting"))
    ctl.tick()
    assert ctl.state.connection == "reconnecting"
    assert ctl.state.message == "RF Explorer lost; reconnecting"
    events.put(LinkEvent("connected", "RF Explorer back"))
    ctl.tick()
    assert ctl.state.connection == "connected"
    events.put(LinkEvent("error", "device did not confirm"))
    ctl.tick()
    assert ctl.state.error == "device did not confirm"


# --- live mode --------------------------------------------------------------------------------


def test_start_without_a_device_explains(ctl: Controller) -> None:
    ctl.start()
    assert not ctl.state.running
    assert "Connect" in ctl.state.message


def test_live_mode_feeds_traces_and_waterfall(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_range(600 * MHZ, 700 * MHZ)
    ctl.start()
    assert ctl.state.running
    v = ctl.state.trace_version
    run_until(ctl, lambda: ctl.state.waterfall.count >= 3)
    live = ctl.state.traces.live
    assert live is not None
    assert live.start_hz == pytest.approx(600 * MHZ, abs=1 * MHZ)
    assert live.stop_hz == pytest.approx(700 * MHZ, abs=1 * MHZ)
    assert ctl.state.trace_version > v
    assert ctl.state.view_range_hz == (live.start_hz, live.stop_hz)
    assert ctl.state.waterfall.range_hz == (live.start_hz, live.stop_hz)


def test_live_range_is_clamped_by_the_device() -> None:
    c = Controller(
        Factory(lambda: SimulatedLink(sweep_points=512, sweep_interval_s=0.01)),
        port_lister=lambda: [],
        simulator=True,
    )
    connected(c)
    c.set_range(470 * MHZ, 960 * MHZ)
    c.start()
    run_until(c, lambda: c.state.traces.live is not None)
    live = c.state.traces.live
    assert live is not None and len(live.freqs_hz) == 512
    assert live.stop_hz == pytest.approx(470 * MHZ + 342_370_000, abs=1 * MHZ)
    c.shutdown()


def test_live_drops_sweeps_from_before_a_retune(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_range(600 * MHZ, 700 * MHZ)
    ctl.start()
    run_until(ctl, lambda: ctl.state.traces.live is not None)
    ctl.set_range(800 * MHZ, 850 * MHZ)
    seen: list[tuple[int, int]] = []

    def new_axis() -> bool:
        live = ctl.state.traces.live
        assert live is not None
        seen.append((live.start_hz, live.stop_hz))
        return abs(live.start_hz - 800 * MHZ) < MHZ

    run_until(ctl, new_axis)
    assert set(seen) <= {seen[0], seen[-1]}  # only the old and the new axis, nothing else


def test_stop_holds_the_device(ctl: Controller, factory: Factory) -> None:
    connected(ctl)
    ctl.start()
    run_until(ctl, lambda: ctl.state.waterfall.count >= 1)
    ctl.stop()
    assert not ctl.state.running
    for _ in range(5):
        ctl.tick()
        time.sleep(0.01)
    count = ctl.state.waterfall.count
    time.sleep(0.1)
    ctl.tick()
    assert ctl.state.waterfall.count == count


def test_toggle_starts_and_stops(ctl: Controller) -> None:
    connected(ctl)
    ctl.toggle()
    assert ctl.state.running
    ctl.toggle()
    assert not ctl.state.running


def test_reset_max_hold(ctl: Controller) -> None:
    connected(ctl)
    ctl.start()
    run_until(ctl, lambda: ctl.state.traces.max_hold is not None)
    v = ctl.state.trace_version
    ctl.reset_max_hold()
    assert ctl.state.traces.max_hold is None
    assert ctl.state.trace_version > v


def test_sweep_rate_is_measured(ctl: Controller) -> None:
    connected(ctl)
    ctl.start()
    run_until(ctl, lambda: ctl.state.sweeps_per_s > 0, timeout=4.0)


# --- scan mode --------------------------------------------------------------------------------


def test_scan_runs_to_completion_and_becomes_a_trace(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_mode("scan")
    ctl.set_resolution(Resolution.FAST)
    ctl.set_range(470 * MHZ, 520 * MHZ)
    assert ctl.state.scan_estimate_s is not None and ctl.state.scan_estimate_s > 0
    ctl.start()
    assert ctl.state.running
    partials: list[Any] = []

    def done() -> bool:
        if ctl.state.scan_partial is not None:
            partials.append(ctl.state.scan_partial)
        return not ctl.state.busy

    run_until(ctl, done, timeout=10.0)
    assert partials, "the partial stitched trace was shown while scanning"
    live = ctl.state.traces.live
    assert live is not None
    assert live.start_hz == pytest.approx(470 * MHZ, abs=1 * MHZ)
    assert live.stop_hz == pytest.approx(520 * MHZ, abs=1 * MHZ)
    assert len(live.freqs_hz) > 112
    assert ctl.state.scan_partial is None
    assert ctl.state.waterfall.count == 1
    assert ctl.state.view_range_hz == (live.start_hz, live.stop_hz)
    assert "Scan done" in ctl.state.message


def test_repeated_scans_accumulate_max_hold(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_mode("scan")
    ctl.set_resolution(Resolution.FAST)
    ctl.set_range(470 * MHZ, 480 * MHZ)
    for _ in range(2):
        ctl.start()
        run_until(ctl, lambda: not ctl.state.busy, timeout=10.0)
    assert ctl.state.waterfall.count == 2
    assert ctl.state.traces.max_hold is not None


def test_scan_cancel_restores_without_a_result(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_mode("scan")
    ctl.set_range(470 * MHZ, 960 * MHZ)
    ctl.start()
    ctl.tick()
    ctl.stop()
    assert ctl.state.stopping and not ctl.state.running
    run_until(ctl, lambda: not ctl.state.busy, timeout=10.0)
    assert ctl.state.traces.live is None
    assert ctl.state.scan_partial is None
    assert "stopped" in ctl.state.message.lower()


def test_scan_outside_the_device_range_is_refused(ctl: Controller) -> None:
    connected(ctl)
    ctl.set_mode("scan")
    ctl.set_range(1785 * MHZ, 1805 * MHZ)
    ctl.start()
    assert not ctl.state.running
    assert "outside" in ctl.state.message


def test_switching_mode_stops_acquisition(ctl: Controller) -> None:
    connected(ctl)
    ctl.start()
    ctl.set_mode("scan")
    assert ctl.state.mode == "scan" and not ctl.state.running


# --- range, presets, settings -----------------------------------------------------------------


def test_presets_set_the_range(ctl: Controller) -> None:
    ctl.set_preset("694-790")
    assert (ctl.state.start_hz, ctl.state.stop_hz) == (694 * MHZ, 790 * MHZ)
    assert ctl.state.preset == "694-790"
    ctl.set_range(700 * MHZ, 710 * MHZ)
    assert ctl.state.preset is None


def test_presets_follow_the_device_range(ctl: Controller) -> None:
    assert "1785-1805" in [p.name for p in ctl.available_presets()]
    connected(ctl)
    names = [p.name for p in ctl.available_presets()]
    assert "1785-1805" not in names and names[-1] == "Overview"
    ctl.set_preset("1785-1805")
    assert ctl.state.preset != "1785-1805"
    ctl.set_preset("Overview")
    caps = ctl.state.capabilities
    assert caps is not None
    assert (ctl.state.start_hz, ctl.state.stop_hz) == (caps.min_hz, caps.max_hz)


def test_center_span(ctl: Controller) -> None:
    ctl.set_center_span(600 * MHZ, 20 * MHZ)
    assert (ctl.state.start_hz, ctl.state.stop_hz) == (590 * MHZ, 610 * MHZ)
    assert (ctl.state.center_hz, ctl.state.span_hz) == (600 * MHZ, 20 * MHZ)


def test_invalid_range_is_ignored(ctl: Controller) -> None:
    ctl.set_range(700 * MHZ, 600 * MHZ)
    assert (ctl.state.start_hz, ctl.state.stop_hz) == (470 * MHZ, 960 * MHZ)
    assert "greater" in ctl.state.message


def test_settings_are_applied_and_exported(factory: Factory) -> None:
    s = AppSettings(
        preset=None,
        resolution="fine",
        start_hz=600 * MHZ,
        stop_hz=650 * MHZ,
        waterfall_depth=50,
        mode="scan",
        auto_connect=True,
    )
    c = Controller(factory, settings=s, port_lister=lambda: [])
    assert c.state.resolution is Resolution.FINE
    assert (c.state.start_hz, c.state.stop_hz) == (600 * MHZ, 650 * MHZ)
    assert c.state.waterfall.depth == 50
    assert c.state.mode == "scan"
    assert c.state.auto_connect
    c.set_waterfall_depth(80)
    c.set_auto_connect(False)
    c.set_preset("823-832")
    out = c.current_settings(s)
    assert out.waterfall_depth == 80
    assert out.auto_connect is False
    assert out.preset == "823-832"
    assert (out.start_hz, out.stop_hz) == (823 * MHZ, 832 * MHZ)


def test_unknown_preset_in_settings_keeps_the_range(factory: Factory) -> None:
    s = AppSettings(preset="gone", start_hz=600 * MHZ, stop_hz=650 * MHZ)
    c = Controller(factory, settings=s, port_lister=lambda: [])
    assert c.state.preset is None
    assert (c.state.start_hz, c.state.stop_hz) == (600 * MHZ, 650 * MHZ)


def test_ui_version_bumps_on_intents(ctl: Controller) -> None:
    v = ctl.state.ui_version
    ctl.set_resolution(Resolution.FINE)
    assert ctl.state.ui_version > v


def test_non_finite_sweeps_are_skipped(ctl: Controller, factory: Factory) -> None:
    connected(ctl)
    ctl.start()
    run_until(ctl, lambda: ctl.state.traces.live is not None)
    live = ctl.state.traces.live
    assert live is not None
    bad = Sweep(live.freqs_hz, live.dbm * float("nan"), 0.0)
    factory.links[0].sweeps.put(bad)
    for _ in range(3):
        ctl.tick()  # must not raise


def test_a_preset_the_device_cannot_tune_becomes_a_custom_range(factory: Factory) -> None:
    s = AppSettings(preset="1785-1805")
    c = Controller(factory, settings=s, port_lister=lambda: [], simulator=True)
    assert c.state.preset == "1785-1805"
    connected(c)
    assert c.state.preset is None
    assert (c.state.start_hz, c.state.stop_hz) == (1785 * MHZ, 1805 * MHZ)
    c.shutdown()


def test_reconnect_waits_until_the_port_is_closed() -> None:
    port = ExclusivePort()
    factory = Factory(lambda: ExclusiveLink(port))
    c = Controller(factory, port_lister=lambda: [])
    connected(c)
    c.disconnect()
    c.connect("/dev/ttyUSB0")  # too early: the old link still holds the port
    assert len(factory.links) == 1
    assert "previous connection" in c.state.message
    run_until(c, lambda: c.state.connection == "disconnected")
    assert not port.held
    c.connect("/dev/ttyUSB0")
    run_until(c, lambda: c.state.connection == "connected")
    assert c.state.error is None and port.opens == 2
    c.shutdown()


def test_disconnect_during_connect_closes_the_late_link() -> None:
    port = ExclusivePort()
    factory = Factory(lambda: ExclusiveLink(port, connect_delay_s=0.3))
    c = Controller(factory, port_lister=lambda: [])
    c.connect("/dev/ttyUSB0")
    c.disconnect()
    assert c.state.connection == "disconnecting"
    c.connect("/dev/ttyUSB0")  # refused: the first open() is still running
    assert len(factory.links) == 1
    run_until(c, lambda: c.state.connection == "disconnected")
    assert not factory.links[0].is_open and not port.held
    c.connect("/dev/ttyUSB0")
    run_until(c, lambda: c.state.connection == "connected")
    assert port.opens == 2
    c.shutdown()


def test_failed_abandoned_connect_releases_the_wait() -> None:
    factory = Factory(FailingLink)
    c = Controller(factory, port_lister=lambda: [])
    c.connect("/dev/ttyUSB0")
    c.disconnect()
    run_until(c, lambda: c.state.connection == "disconnected")


def test_shutdown_during_connect_closes_the_link_after_open() -> None:
    factory = Factory(lambda: SimulatedLink(connect_delay_s=0.2))
    c = Controller(factory, port_lister=lambda: [], simulator=True)
    c.connect()
    c.shutdown()
    link = factory.links[0]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (link.model is None or link.is_open):
        time.sleep(0.01)
    assert link.model is not None, "open() finished"
    assert not link.is_open


def test_link_thread_dying_disconnects(ctl: Controller, factory: Factory) -> None:
    connected(ctl)
    link = factory.links[0]
    assert isinstance(link, SimulatedLink)
    link._stop.set()  # the worker thread ends without a "disconnected" event
    assert link._thread is not None
    link._thread.join(timeout=2)
    run_until(ctl, lambda: ctl.state.connection == "disconnected")
    assert ctl.state.error == "The device link closed"
    assert ctl.state.message == "Device disconnected"


def test_waterfall_depth_is_clamped(ctl: Controller) -> None:
    ctl.set_waterfall_depth(5)
    assert ctl.state.waterfall.depth == 10
    ctl.set_waterfall_depth(10_000)
    assert ctl.state.waterfall.depth == 1000
