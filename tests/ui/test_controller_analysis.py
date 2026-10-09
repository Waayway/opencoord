"""Controller: channel overlay, exclusion zones, analysis, amplitude offset, module switch."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import replace

import numpy as np
import pytest

from opencoord.core.settings import AppSettings
from opencoord.core.types import Sweep
from opencoord.core.zones import MAX_EXCLUSION_ZONES
from opencoord.device.link_api import Link
from opencoord.device.models import Capabilities
from opencoord.device.simulator import SimulatedLink
from opencoord.ui.controller import Controller

MHZ = 1_000_000


def run_until(c: Controller, cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; state message: {c.state.message!r}")
        c.tick()
        time.sleep(0.005)


def connected(c: Controller) -> Controller:
    c.connect()
    run_until(c, lambda: c.state.connection == "connected")
    return c


def Factory() -> Callable[[str | None], Link]:
    return lambda _port: SimulatedLink(seed=1, sweep_interval_s=0.01)


def _sweep(levels: dict[float, float], start_mhz: float = 470.0, n: int = 241) -> Sweep:
    """A 0.1 MHz raster from ``start_mhz``; ``levels`` maps MHz to a peak level over -100 dBm."""
    freqs = start_mhz * 1e6 + 100_000.0 * np.arange(n, dtype=np.float64)
    dbm = np.full(n, -100.0, dtype=np.float32)
    for mhz, level in levels.items():
        dbm[round((mhz - start_mhz) * 10)] = level
    return Sweep(freqs, dbm, 0.0)


@pytest.fixture
def c() -> Iterator[Controller]:
    ctl = Controller(Factory(), port_lister=lambda: [], simulator=True)
    yield ctl
    ctl.shutdown()


# --- overlay / plan -----------------------------------------------------------------------------


def test_default_plan_is_eu_and_overlay_starts_hidden(c: Controller) -> None:
    assert c.state.channel_plan is not None and c.state.channel_plan.name == "eu"
    assert c.state.overlay_enabled is False


def test_overlay_toggle_and_plan_selection(c: Controller) -> None:
    v = c.state.ui_version
    c.set_overlay_enabled(True)
    assert c.state.overlay_enabled and c.state.ui_version > v
    c.set_channel_plan("nope")
    assert c.state.channel_plan is not None and c.state.channel_plan.name == "eu"
    assert "nope" in c.state.message


# --- analysis -----------------------------------------------------------------------------------


def test_analysis_needs_a_trace(c: Controller) -> None:
    assert c.analysis() is None


def test_carriers_use_floor_plus_ten_db_by_default(c: Controller) -> None:
    # A carrier at 475.0 MHz (channel 21) and a weak one 5 dB over the floor at 600.0 MHz.
    c.state.traces.update(_sweep({475.0: -50.0, 480.5: -95.0}))
    c.state.trace_version += 1
    a = c.analysis()
    assert a is not None
    assert a.floor_dbm == pytest.approx(-100.0)
    assert a.threshold_db == pytest.approx(10.0)
    assert [(r.carrier.freq_hz, r.channel) for r in a.carriers] == [(475 * MHZ, 21)]
    assert a.carriers[0].carrier.level_dbm == pytest.approx(-50.0)


def test_carriers_use_the_threshold_line_when_set(c: Controller) -> None:
    c.state.traces.update(_sweep({475.0: -50.0, 480.5: -95.0}))
    c.set_threshold_dbm(-97.0)
    c.state.trace_version += 1
    a = c.analysis()
    assert a is not None
    assert a.threshold_db == pytest.approx(3.0)
    assert [r.carrier.freq_hz for r in a.carriers] == [475 * MHZ, round(480.5 * MHZ)]


def test_carrier_outside_the_plan_has_no_channel(c: Controller) -> None:
    c.state.traces.update(_sweep({700.0: -40.0}, start_mhz=690.0))
    c.state.trace_version += 1
    a = c.analysis()
    assert a is not None and a.carriers[0].channel is None


def test_occupancy_per_channel(c: Controller) -> None:
    c.state.traces.update(_sweep({475.0: -50.0}))  # 470..494.0 MHz = channels 21-23
    c.state.trace_version += 1
    a = c.analysis()
    assert a is not None
    by_ch = {o.number: o for o in a.occupancy}
    assert by_ch[21].max_dbm == pytest.approx(-50.0)
    assert by_ch[21].percent_above > 0
    assert by_ch[22].percent_above == 0


def test_analysis_is_cached_until_the_trace_changes(c: Controller) -> None:
    c.state.traces.update(_sweep({475.0: -50.0}))
    c.state.trace_version += 1
    first = c.analysis()
    assert c.analysis() is first
    c.state.trace_version += 1
    assert c.analysis() is not first


def test_analysis_without_plan_still_lists_carriers(c: Controller) -> None:
    c.state.channel_plan = None
    c.state.traces.update(_sweep({475.0: -50.0}))
    c.state.trace_version += 1
    a = c.analysis()
    assert a is not None and a.occupancy == () and a.carriers[0].channel is None


# --- exclusion zones ----------------------------------------------------------------------------


def test_add_edit_remove_exclusion_zone(c: Controller) -> None:
    v = c.state.ui_version
    zid = c.add_exclusion_zone(520 * MHZ, 510 * MHZ)  # reversed order is normalised
    assert zid == 1 and c.state.ui_version > v
    z = c.state.exclusion_zones[0]
    assert (z.start_hz, z.stop_hz) == (510 * MHZ, 520 * MHZ)
    c.update_exclusion_zone(1, 600 * MHZ, 610 * MHZ)
    assert (c.state.exclusion_zones[0].start_hz, c.state.exclusion_zones[0].stop_hz) == (
        600 * MHZ,
        610 * MHZ,
    )
    assert c.add_exclusion_zone(1 * MHZ, 2 * MHZ) == 2
    c.remove_exclusion_zone(1)
    assert [z.id for z in c.state.exclusion_zones] == [2]
    assert c.add_exclusion_zone(3 * MHZ, 4 * MHZ) == 1  # smallest free id
    c.clear_exclusion_zones()
    assert c.state.exclusion_zones == []


def test_empty_zone_is_rejected(c: Controller) -> None:
    assert c.add_exclusion_zone(500 * MHZ, 500 * MHZ) is None
    assert c.add_exclusion_zone(-5, 10) is None
    assert c.state.exclusion_zones == []
    assert "zone" in c.state.message.lower()
    c.add_exclusion_zone(1 * MHZ, 2 * MHZ)
    c.update_exclusion_zone(1, 5, 5)  # refused, unchanged
    assert c.state.exclusion_zones[0].stop_hz == 2 * MHZ


def test_zone_limit(c: Controller) -> None:
    for i in range(MAX_EXCLUSION_ZONES):
        assert c.add_exclusion_zone(i * MHZ, i * MHZ + 1000) is not None
    assert c.add_exclusion_zone(100 * MHZ, 101 * MHZ) is None
    assert str(MAX_EXCLUSION_ZONES) in c.state.message


# --- amplitude offset ---------------------------------------------------------------------------


def test_amp_offset_needs_a_device(c: Controller) -> None:
    assert c.amp_offset_db == 0.0
    c.set_amp_offset_db(3.0)
    assert c.state.amp_offsets == {}
    assert "Connect" in c.state.message


def test_amp_offset_is_applied_to_live_sweeps(c: Controller) -> None:
    connected(c)
    c.set_range(470 * MHZ, 500 * MHZ)
    c.start()
    run_until(c, lambda: c.state.traces.live is not None)
    c.set_amp_offset_db(6.0)
    assert c.state.traces.live is None  # history reset: it was recorded with another offset
    link = c._link
    assert link is not None and link.config is not None
    link.hold()
    cfg = link.config
    freqs = cfg.start_hz + cfg.step_hz * np.arange(cfg.sweep_points, dtype=np.float64)
    link.sweeps.put(Sweep(freqs, np.full(cfg.sweep_points, -80.0, dtype=np.float32), 0.0))
    run_until(c, lambda: c.state.traces.live is not None)
    live = c.state.traces.live
    assert live is not None and float(live.dbm[0]) == pytest.approx(-74.0)


def test_amp_offset_is_stored_per_model_and_exported() -> None:
    ctl = Controller(
        Factory(),
        settings=AppSettings(amp_offsets={"model_10": 2.5, "model_3": -1.0}),
        port_lister=lambda: [],
        simulator=True,
    )
    try:
        assert ctl.amp_offset_db == 0.0  # not connected: no model yet
        connected(ctl)
        assert ctl.amp_offset_db == 2.5
        ctl.set_amp_offset_db(4.0)
        out = ctl.current_settings(AppSettings())
        assert out.amp_offsets == {"model_10": 4.0, "model_3": -1.0}
    finally:
        ctl.shutdown()


def test_amp_offset_is_validated(c: Controller) -> None:
    connected(c)
    c.set_amp_offset_db(float("nan"))
    c.set_amp_offset_db(500.0)
    assert c.amp_offset_db == 0.0
    c.set_amp_offset_db(0.0)
    assert c.state.amp_offsets == {}  # zero is the default and is not stored


def test_amp_offset_applies_to_scans() -> None:
    from opencoord.device.scanner import Resolution

    ctl = Controller(Factory(), port_lister=lambda: [], simulator=True)
    try:
        connected(ctl)
        ctl.set_resolution(Resolution.FAST)
        ctl.set_range(470 * MHZ, 480 * MHZ)
        ctl.set_amp_offset_db(10.0)
        ctl.set_mode("scan")
        ctl.start()
        run_until(ctl, lambda: not ctl.state.busy and ctl.state.traces.live is not None, 8)
        offset = ctl.state.traces.live
        assert offset is not None
        ctl.set_amp_offset_db(0.0)
        ctl.start()
        run_until(ctl, lambda: not ctl.state.busy and ctl.state.traces.live is not None, 8)
        plain = ctl.state.traces.live
        assert plain is not None
        # Same simulated band (noise differs per scan): the mean level moves by about 10 dB.
        assert float(offset.dbm.mean() - plain.dbm.mean()) == pytest.approx(10.0, abs=1.5)
    finally:
        ctl.shutdown()


# --- module switch ------------------------------------------------------------------------------


class ExpansionLink(SimulatedLink):
    def __init__(self) -> None:
        super().__init__(sweep_interval_s=0.01)
        self.switched: list[bool] = []

    @property
    def capabilities(self) -> Capabilities | None:
        caps = super().capabilities
        return None if caps is None else replace(caps, expansion_name="RF Explorer 2.4G")

    def switch_module(self, main: bool) -> None:
        self.switched.append(main)


def test_switch_module_calls_the_link() -> None:
    made: list[ExpansionLink] = []

    def make() -> Link:
        made.append(ExpansionLink())
        return made[-1]

    ctl = Controller(lambda _p: make(), port_lister=lambda: [], simulator=True)
    try:
        connected(ctl)
        caps = ctl.state.capabilities
        assert caps is not None and caps.expansion_name is not None
        ctl.start()
        ctl.switch_module(False)
        assert made[0].switched == [False]
        assert not ctl.state.running  # acquisition stops; the band changes
        assert "expansion" in ctl.state.message.lower()
        ctl.switch_module(True)
        assert made[0].switched == [False, True]
    finally:
        ctl.shutdown()


def test_switch_module_without_expansion_is_refused(c: Controller) -> None:
    connected(c)
    c.switch_module(False)
    assert "expansion" in c.state.message.lower()


def test_switch_module_without_device(c: Controller) -> None:
    c.switch_module(False)
    assert "Connect" in c.state.message
