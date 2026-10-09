"""Controller marker, threshold, reference-trace and auto-scale intents (no display needed)."""

from __future__ import annotations

import numpy as np
import pytest

from opencoord.core.markers import MAX_MARKERS
from opencoord.core.settings import AppSettings
from opencoord.core.types import Sweep, Trace
from opencoord.device.simulator import SimulatedLink
from opencoord.ui.controller import MAX_REFERENCES, Controller

MHZ = 1_000_000
START = 470 * MHZ


def _sweep(dbm: list[float]) -> Sweep:
    freqs = START + 100_000.0 * np.arange(len(dbm), dtype=np.float64)
    return Sweep(freqs, np.asarray(dbm, dtype=np.float32), 0.0)


@pytest.fixture
def c() -> Controller:
    ctl = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [])
    # Two sweeps: max hold keeps the peaks of both, live is the second one.
    ctl.state.traces.update(_sweep([-100, -60, -100, -100, -70, -100, -100, -50, -100, -100]))
    ctl.state.traces.update(_sweep([-100, -100, -100, -100, -90, -100, -100, -100, -100, -100]))
    return ctl


def test_add_marker_selects_and_bumps_version(c: Controller) -> None:
    v = c.state.ui_version
    mid = c.add_marker(START + 100_000)
    assert mid == 1
    assert c.state.selected_marker == 1
    assert c.state.markers[0].freq_hz == START + 100_000
    assert c.state.markers[0].trace_key == "max"
    assert c.state.ui_version > v


def test_marker_ids_reuse_the_smallest_free(c: Controller) -> None:
    for _ in range(3):
        c.add_marker(START)
    c.remove_marker(2)
    assert c.add_marker(START) == 2


def test_at_most_eight_markers(c: Controller) -> None:
    for _ in range(MAX_MARKERS):
        assert c.add_marker(START) is not None
    assert c.add_marker(START) is None
    assert len(c.state.markers) == MAX_MARKERS
    assert "8" in c.state.message


def test_remove_marker_clears_selection_and_delta_reference(c: Controller) -> None:
    c.add_marker(START)
    c.add_marker(START + 400_000)
    c.set_delta_reference(2)
    c.remove_marker(2)
    assert [m.id for m in c.state.markers] == [1]
    assert c.state.delta_reference is None
    assert c.state.selected_marker is None
    c.remove_marker(99)  # unknown: no-op


def test_clear_markers(c: Controller) -> None:
    c.add_marker(START)
    c.set_delta_reference(1)
    v = c.state.ui_version
    c.clear_markers()
    assert c.state.markers == []
    assert c.state.selected_marker is None and c.state.delta_reference is None
    assert c.state.ui_version > v


def test_select_marker(c: Controller) -> None:
    c.add_marker(START)
    c.add_marker(START)
    c.select_marker(1)
    assert c.state.selected_marker == 1
    c.select_marker(77)  # unknown ids are ignored
    assert c.state.selected_marker == 1
    c.select_marker(None)
    assert c.state.selected_marker is None


def test_move_marker(c: Controller) -> None:
    c.add_marker(START)
    c.move_marker(1, START + 700_000)
    assert c.state.markers[0].freq_hz == START + 700_000
    assert c.state.markers[0].id == 1


def test_marker_to_peak_uses_the_marker_trace(c: Controller) -> None:
    c.add_marker(START)
    c.marker_to_peak()
    assert c.state.markers[0].freq_hz == START + 700_000  # max hold peak (-50 dBm)


def test_marker_to_peak_needs_a_selection(c: Controller) -> None:
    c.marker_to_peak()
    assert "marker" in c.state.message.lower()


def test_marker_next_peak(c: Controller) -> None:
    c.add_marker(START + 100_000)
    c.marker_next_peak("right")
    assert c.state.markers[0].freq_hz == START + 400_000
    c.marker_next_peak("right")
    assert c.state.markers[0].freq_hz == START + 700_000
    c.marker_next_peak("right")  # nothing further: stays, says so
    assert c.state.markers[0].freq_hz == START + 700_000
    assert "peak" in c.state.message.lower()
    c.marker_next_peak("left")
    assert c.state.markers[0].freq_hz == START + 400_000


def test_marker_at_cursor_or_peak(c: Controller) -> None:
    c.state.cursor_hz = START + 250_000
    c.add_marker_at_cursor()
    assert c.state.markers[0].freq_hz == START + 250_000
    c.state.cursor_hz = None
    c.add_marker_at_cursor()
    assert c.state.markers[1].freq_hz == START + 700_000


def test_marker_without_data_is_refused() -> None:
    ctl = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [])
    ctl.add_marker_at_cursor()
    assert ctl.state.markers == []


def test_marker_rows_levels_and_delta(c: Controller) -> None:
    c.add_marker(START + 100_000)  # max hold: -60
    c.add_marker(START + 700_000)  # max hold: -50
    c.set_delta_reference(1)
    rows = c.marker_rows()
    assert [r.level_dbm for r in rows] == [-60.0, -50.0]
    assert rows[0].delta is None  # the reference itself
    assert rows[1].delta == (600_000, pytest.approx(10.0))
    c.set_delta_reference(None)
    assert all(r.delta is None for r in c.marker_rows())


def test_set_delta_reference_ignores_unknown_ids(c: Controller) -> None:
    c.add_marker(START)
    c.set_delta_reference(5)
    assert c.state.delta_reference is None


def test_marker_falls_back_when_its_trace_is_gone(c: Controller) -> None:
    c.add_marker(START + 100_000)
    c.freeze_reference()
    c.state.markers[0] = type(c.state.markers[0])(1, START + 100_000, "ref1")
    c.remove_reference(0)
    assert c.marker_rows()[0].level_dbm == -60.0  # reads max hold again


def test_threshold(c: Controller) -> None:
    v = c.state.ui_version
    c.set_threshold_dbm(-75.5)
    assert c.state.threshold_dbm == -75.5
    assert c.state.ui_version > v
    c.set_threshold_dbm(None)
    assert c.state.threshold_dbm is None
    c.set_threshold_dbm(float("nan"))  # ignored
    assert c.state.threshold_dbm is None


def test_threshold_is_stored_in_settings() -> None:
    ctl = Controller(
        lambda _p: SimulatedLink(),
        port_lister=lambda: [],
        settings=AppSettings(threshold_dbm=-80.0),
    )
    assert ctl.state.threshold_dbm == -80.0
    ctl.set_threshold_dbm(-70.0)
    assert ctl.current_settings(AppSettings()).threshold_dbm == -70.0


def test_freeze_reference_copies_the_primary_trace(c: Controller) -> None:
    tv = c.state.trace_version
    assert c.freeze_reference() == "ref1"
    ref = c.state.references["ref1"]
    max_hold = c.state.traces.max_hold
    assert max_hold is not None
    assert np.array_equal(ref.dbm, max_hold.dbm)
    assert ref.dbm is not max_hold.dbm
    assert ref.label.startswith("Ref 1")
    assert c.state.trace_version > tv
    # later sweeps do not change the frozen copy
    c.state.traces.update(_sweep([-40.0] * 10))
    assert float(np.max(ref.dbm)) == -50.0


def test_freeze_reference_up_to_four_then_refuses(c: Controller) -> None:
    keys = [c.freeze_reference() for _ in range(MAX_REFERENCES)]
    assert keys == ["ref1", "ref2", "ref3", "ref4"]
    assert c.freeze_reference() is None
    assert "4" in c.state.message
    assert len(c.state.references) == 4


def test_freeze_reference_without_data() -> None:
    ctl = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [])
    assert ctl.freeze_reference() is None
    assert ctl.state.references == {}


def test_remove_reference_by_index_frees_its_key(c: Controller) -> None:
    c.freeze_reference()
    c.freeze_reference()
    c.set_trace_visible("ref1", False)
    c.remove_reference(0)
    assert list(c.state.references) == ["ref2"]
    assert "ref1" not in c.state.hidden_traces
    assert c.freeze_reference() == "ref1"
    c.remove_reference(9)  # out of range: no-op
    assert len(c.state.references) == 2


def test_trace_visibility(c: Controller) -> None:
    tv, uv = c.state.trace_version, c.state.ui_version
    c.set_trace_visible("live", False)
    assert c.state.hidden_traces == {"live"}
    assert c.state.trace_version > tv and c.state.ui_version > uv
    c.set_trace_visible("live", True)
    assert c.state.hidden_traces == set()


def test_auto_scale_uses_visible_traces_with_padding(c: Controller) -> None:
    c.auto_scale()
    # all of live/avg/min/max: lowest -100, highest -50 (max hold)
    assert c.state.y_limits == (-105.0, -45.0)
    version = c.state.y_limits_version
    c.set_trace_visible("max", False)
    c.set_trace_visible("avg", False)
    c.auto_scale()
    assert c.state.y_limits == (-105.0, -85.0)  # live peaks at -90
    assert c.state.y_limits_version > version


def test_auto_scale_includes_visible_references(c: Controller) -> None:
    ref = Trace(np.asarray([START, START + 1.0]), np.asarray([-30.0, -40.0], dtype=np.float32), "r")
    c.state.references["ref1"] = ref
    c.auto_scale()
    assert c.state.y_limits is not None and c.state.y_limits[1] == -25.0


def test_auto_scale_without_data() -> None:
    ctl = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [])
    ctl.auto_scale()
    assert ctl.state.y_limits is None
    assert "data" in ctl.state.message.lower()
