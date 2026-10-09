import queue
import time

import numpy as np
import pytest

from opencoord.core.types import Sweep
from opencoord.device.link_api import Link, LinkEvent
from opencoord.device.simulator import INTERMITTENT_HZ, SimulatedLink, generate

MHZ = 1_000_000


def test_generate_is_deterministic_float32() -> None:
    a = generate(470 * MHZ, 700 * MHZ, 112, 1.5, 7)
    b = generate(470 * MHZ, 700 * MHZ, 112, 1.5, 7)
    assert a.dtype == np.float32
    assert a.shape == (112,)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, generate(470 * MHZ, 700 * MHZ, 112, 1.5, 8))


def test_generate_noise_floor_and_dvbt_blocks() -> None:
    d = generate(470 * MHZ, 700 * MHZ, 2000, 0.0, 1)
    freqs = np.linspace(470 * MHZ, 700 * MHZ, 2000)
    ch27 = d[np.abs(freqs - 522 * MHZ) < 3 * MHZ]
    assert np.all(ch27 > -66) and np.all(ch27 < -54)
    empty = d[np.abs(freqs - 650 * MHZ) < 1 * MHZ]  # gap between blocks and carriers
    assert -115 < float(np.median(empty)) < -95


def test_generate_narrowband_carrier_in_wireless_mic_band() -> None:
    d = generate(823 * MHZ, 832 * MHZ, 900, 0.0, 1)
    assert float(d.max()) > -70


def test_generate_intermittent_carrier_toggles_with_time() -> None:
    def level(t: float) -> float:
        d = generate(INTERMITTENT_HZ - MHZ, INTERMITTENT_HZ + MHZ, 41, t, 1)
        return float(d.max())

    assert level(0.5) > -70  # on
    assert level(2.5) < -90  # off
    assert level(0.5) > -70 and level(4.5) > -70  # periodic


def test_generate_low_resolution_still_sees_carriers() -> None:
    d = generate(50_000, 960 * MHZ, 112, 0.0, 1)
    assert float(d.max()) > -70


@pytest.fixture
def link() -> SimulatedLink:
    lk = SimulatedLink(seed=3, sweep_interval_s=0.01)
    yield lk
    lk.close()


def test_conforms_to_link_protocol(link: SimulatedLink) -> None:
    api: Link = link
    assert not api.is_open
    assert api.config is None and api.model is None and api.capabilities is None


def test_open_reports_model_config_and_connected_event(link: SimulatedLink) -> None:
    link.open()
    assert link.is_open
    assert link.events.get(timeout=2) == LinkEvent(
        "connected", "Simulated RF Explorer WSUB1G+ connected"
    )
    assert link.model is not None and link.model.main_code == 10
    assert link.model.firmware == "03.39"
    cfg = link.config
    assert cfg is not None
    assert cfg.sweep_points == 112
    assert (cfg.min_hz, cfg.max_hz, cfg.max_span_hz) == (50_000, 960 * MHZ, 959_950_000)
    caps = link.capabilities
    assert caps is not None and caps.is_plus and caps.max_hz == 960 * MHZ


def test_streams_sweeps_matching_config(link: SimulatedLink) -> None:
    link.open()
    s = link.sweeps.get(timeout=2)
    assert isinstance(s, Sweep)
    assert len(s.dbm) == 112 and s.dbm.dtype == np.float32
    assert s.start_hz == link.config.start_hz  # type: ignore[union-attr]


def test_set_span_clamps_and_changes_config(link: SimulatedLink) -> None:
    link.open()
    link.set_span(470 * MHZ, 700 * MHZ)
    cfg = link.config
    assert cfg is not None
    assert cfg.start_hz == 470 * MHZ
    assert cfg.sweep_points == 112
    assert cfg.step_hz == round(230 * MHZ / 111)
    link.set_span(1, 5000 * MHZ)
    cfg = link.config
    assert cfg is not None
    assert cfg.start_hz == 50_000
    assert cfg.stop_hz <= 960 * MHZ
    # drain until a sweep from the new config arrives
    for _ in range(100):
        s = link.sweeps.get(timeout=2)
        if s.start_hz == 50_000:
            break
    else:
        pytest.fail("no sweep for new span")


def test_set_span_rejects_empty_range(link: SimulatedLink) -> None:
    link.open()
    with pytest.raises(ValueError):
        link.set_span(500 * MHZ, 500 * MHZ)


def test_hold_stops_sweeps_and_set_span_resumes(link: SimulatedLink) -> None:
    link.open()
    link.sweeps.get(timeout=2)
    link.hold()
    # allow an in-flight sweep to land, then drain
    time.sleep(0.1)
    while True:
        try:
            link.sweeps.get_nowait()
        except queue.Empty:
            break
    with pytest.raises(queue.Empty):
        link.sweeps.get(timeout=0.15)
    link.set_span(500 * MHZ, 600 * MHZ)
    assert link.sweeps.get(timeout=2).start_hz == 500 * MHZ


def test_sweep_queue_is_bounded_dropping_oldest() -> None:
    lk = SimulatedLink(seed=1, sweep_interval_s=0.0005, queue_size=4)
    try:
        lk.open()
        time.sleep(0.15)
        assert lk.sweeps.qsize() <= 4
        stamps = []
        while not lk.sweeps.empty():
            stamps.append(lk.sweeps.get_nowait().timestamp)
        assert stamps == sorted(stamps)
    finally:
        lk.close()


def test_close_stops_thread_and_emits_disconnected(link: SimulatedLink) -> None:
    link.open()
    link.close()
    assert not link.is_open
    kinds = []
    while not link.events.empty():
        kinds.append(link.events.get_nowait().kind)
    assert kinds == ["connected", "disconnected"]
    link.close()  # idempotent


def test_switch_module_without_expansion_reports_error(link: SimulatedLink) -> None:
    link.open()
    link.events.get(timeout=2)
    link.switch_module(False)
    ev = link.events.get(timeout=2)
    assert ev.kind == "error" and "expansion" in ev.message.lower()
    link.switch_module(True)
    assert link.events.empty()
