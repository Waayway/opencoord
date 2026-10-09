"""CoordinationActions: background solve/check, cancel, exports and sessions (no display)."""

from __future__ import annotations

import base64
import csv
import io
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from opencoord.coord.profiles import TuningRange, builtin_templates
from opencoord.coord.solver import CoordinationRequest, Plan, solve
from opencoord.core import session as session_io
from opencoord.core.types import Sweep
from opencoord.device.scanner import Resolution
from opencoord.device.simulator import SimulatedLink
from opencoord.io.export_plan import CSV_HEADER
from opencoord.io.profile_store import ProfileStore
from opencoord.ui.controller import Controller
from opencoord.ui.coordination_actions import CoordinationActions
from opencoord.ui.files import FileActions
from opencoord.ui.profiles_actions import ProfilesActions

MHZ = 1_000_000
GENERIC = "Generic analog mic"


def run_until(c: Controller, cond: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; state message: {c.state.message!r}")
        c.tick()
        time.sleep(0.005)


def wide_generic(store: ProfileStore, name: str = "UHF mic") -> str:
    """The Generic analog template retuned to 470-694 MHz, saved as a user profile."""
    template = builtin_templates()["generic-analog-mic"]
    store.save_profile(replace(template, name=name, tuning=(TuningRange(470 * MHZ, 694 * MHZ),)))
    return name


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[Controller, ProfilesActions, CoordinationActions]]:
    c = Controller(
        lambda _p: SimulatedLink(seed=3, sweep_interval_s=0.005),
        port_lister=lambda: [],
        simulator=True,
    )
    store = ProfileStore(tmp_path / "cfg")
    pa = ProfilesActions(store)
    pa.startup()
    wide_generic(store)
    pa.reload()
    files = FileActions(c)
    ca = CoordinationActions(c, pa, files.say)
    files.coordination = ca
    yield c, pa, ca
    c.shutdown()


def feed_max_hold(c: Controller, carrier_mhz: float) -> None:
    freqs = 470e6 + 25e3 * np.arange(round(224e6 / 25e3) + 1, dtype=np.float64)
    dbm = np.full(freqs.size, -105.0, dtype=np.float32)
    k = round((carrier_mhz * 1e6 - 470e6) / 25e3)
    dbm[k - 4 : k + 5] = -40.0
    c.state.traces.update(Sweep(freqs, dbm, 0.0))
    c.state.trace_version += 1


def test_profiles_offer_user_profiles_then_templates(env) -> None:  # type: ignore[no-untyped-def]
    _c, _pa, ca = env
    names = ca.profile_names()
    assert names[0] == "UHF mic"
    assert GENERIC in names and "Generic IEM" in names
    assert ca.default_lock_preset() == "generic-analog"


def test_coordinate_runs_in_the_background_and_avoids_the_scan(env) -> None:  # type: ignore[no-untyped-def]
    c, _pa, ca = env
    feed_max_hold(c, 500.0)
    ca.model.add_device("UHF mic", 6)
    ca.model.paste_locks("600", label="Venue")
    c.add_exclusion_zone(470 * MHZ, 480 * MHZ)
    v0 = ca.version
    main = threading.get_ident()
    assert ca.coordinate()
    assert ca.running and ca.version > v0
    assert ca.progress_text().startswith("Coordinating... (")
    run_until(c, lambda: not ca.running)
    r = ca.result
    assert r is not None and r.plan.stats.complete and threading.get_ident() == main
    assert r.scan_label == "Max hold" and [lk.label for lk in r.locked] == ["Venue"]
    freqs = [a.freq_hz for a in r.plan.assignments]
    assert len(freqs) == 6
    assert all(abs(f - 500 * MHZ) > 200_000 for f in freqs)  # occupied in the scan
    assert all(not 470 * MHZ <= f <= 480 * MHZ for f in freqs)  # exclusion zone
    assert c.state.message.startswith("Coordinated 6 of 6 devices")
    assert not ca.stale
    assigned, backups = ca.spectrum_lines()
    assert [label for _, label in assigned] == [f"UHF mic #{i}" for i in range(1, 7)]
    assert len(backups) == 2
    ca.set_show_on_spectrum(False)
    assert ca.spectrum_lines() == ([], [])
    ca.model.set_check_text(0, "600")  # check-mode input does not affect the plan
    assert not ca.stale
    ca.model.set_quantity(0, 7)
    assert ca.stale


def test_errors_are_messages(env) -> None:  # type: ignore[no-untyped-def]
    c, _pa, ca = env
    assert not ca.coordinate()
    assert "Add at least one device" in ca.message and ca.message_is_error
    assert c.state.message == ca.message
    assert not ca.check()
    assert "Type or paste frequencies" in ca.message
    assert not ca.export("csv", Path("nowhere.csv"))
    assert "coordinate first" in ca.message
    assert not ca.fill_check_from_result()


def test_cancel_abandons_the_result(env) -> None:  # type: ignore[no-untyped-def]
    c, pa, _ = env
    gate = threading.Event()

    def slow(request: CoordinationRequest) -> Plan:
        gate.wait(5)
        return solve(request)

    ca = CoordinationActions(c, pa, solver=slow)
    ca.model.add_device("UHF mic", 2)
    assert ca.coordinate()
    assert not ca.coordinate()  # one job at a time
    ca.cancel()
    assert not ca.running and ca.message == "Coordination cancelled"
    gate.set()
    time.sleep(0.2)
    for _ in range(5):
        c.tick()
    assert ca.result is None  # the late result was dropped
    # A new run works normally afterwards.
    assert ca.coordinate()
    run_until(c, lambda: not ca.running)
    assert ca.result is not None


def test_solver_crash_is_reported(env) -> None:  # type: ignore[no-untyped-def]
    c, pa, _ = env

    def broken(_request: CoordinationRequest) -> Plan:
        raise RuntimeError("boom")

    ca = CoordinationActions(c, pa, solver=broken)
    ca.model.add_device("UHF mic", 1)
    assert ca.coordinate()
    run_until(c, lambda: not ca.running)
    assert ca.message == "Coordination failed: boom" and ca.result is None


def test_check_mode_lists_violations_and_passes_a_solved_plan(env) -> None:  # type: ignore[no-untyped-def]
    c, _pa, ca = env
    ca.model.add_device("UHF mic", 3)
    ca.model.set_check_text(0, "600.000; 600.100; 610")
    assert ca.check()
    run_until(c, lambda: not ca.running)
    out = ca.check_outcome
    assert out is not None and not out.report.ok
    assert [a.label for a in out.assignments] == ["UHF mic #1", "UHF mic #2", "UHF mic #3"]
    v = next(v for v in out.report.violations if v.rule == "carrier")
    assert (v.required_hz, v.actual_hz) == (350_000, 100_000)
    assert v.sources == ("UHF mic #1", "UHF mic #2")
    assert "1 violation" in ca.message
    # "Edit result": a solved plan copied into the check boxes passes.
    assert ca.coordinate()
    run_until(c, lambda: not ca.running)
    assert ca.fill_check_from_result()
    assert ca.check()
    run_until(c, lambda: not ca.running)
    assert ca.check_outcome is not None and ca.check_outcome.report.ok
    assert ca.message == "Check: no violations among 3 devices"


def test_session_round_trip_keeps_setup_and_plan(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    c, pa, ca = env
    ca.model.add_device("UHF mic", 4)
    ca.model.paste_locks("610.5", label="Venue", preset="iem")
    ca.model.set_threshold_db(14)
    assert ca.coordinate()
    run_until(c, lambda: not ca.running)
    assert ca.result is not None
    f1 = FileActions(c)
    f1.coordination = ca
    path = tmp_path / "show.opencoord"
    assert f1.save(path)

    c2 = Controller(lambda _p: SimulatedLink(), port_lister=lambda: [], simulator=True)
    f2 = FileActions(c2)
    ca2 = CoordinationActions(c2, pa, f2.say)
    f2.coordination = ca2
    s0 = ca2.model.structure_version
    assert f2.open(path)
    assert ca2.model.to_dict() == ca.model.to_dict()
    assert ca2.model.structure_version > s0
    assert ca2.result is not None
    assert ca2.result.plan.assignments == ca.result.plan.assignments
    assert dict(ca2.result.plan.backups) == dict(ca.result.plan.backups)
    assert ca2.result.locked == ca.result.locked and not ca2.stale
    c2.shutdown()


def test_bad_plan_in_a_session_is_ignored_with_a_message(env) -> None:  # type: ignore[no-untyped-def]
    c, _pa, ca = env
    files = FileActions(c)
    files.coordination = ca
    s = session_io.Session(
        coordination={"devices": [{"profile": "UHF mic", "quantity": 2}]},
        plan={"assignments": "nope"},
    )
    files.apply_session(s)
    assert [r.quantity for r in ca.model.rows] == [2]
    assert ca.result is None
    assert "Ignored unreadable parts" in ca.message and "frequency plan" in ca.message
    # A version 1 session (no coordination data) resets the tab.
    files.apply_session(session_io.Session())
    assert ca.model.rows == [] and ca.result is None


def test_exports_write_all_three_formats(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    c, _pa, ca = env
    ca.model.add_device("UHF mic", 2)
    ca.model.set_allow_forbidden(True)
    ca.model.paste_locks("700.5 700.6")  # clashing locked carriers -> a plan warning
    assert ca.coordinate()
    run_until(c, lambda: not ca.running)
    assert ca.result is not None and ca.result.plan.warnings
    warning = ca.result.plan.warnings[0]
    assert ca.export("csv", tmp_path / "plan")
    assert ca.export("txt", tmp_path / "plan")
    rgba = np.full((4, 6, 4), 200, dtype=np.uint8)
    assert ca.export("html", tmp_path / "plan", rgba)
    csv_text = (tmp_path / "plan.csv").read_text()
    assert csv_text.splitlines()[0] == CSV_HEADER
    assert ["warning", warning, "", "", "", ""] in list(csv.reader(io.StringIO(csv_text)))
    assert warning in (tmp_path / "plan.txt").read_text()
    html = (tmp_path / "plan.html").read_text()
    assert warning in html.replace("&#x27;", "'")
    m = re.search(r'src="data:image/png;base64,([^"]+)"', html)
    assert m is not None and base64.b64decode(m.group(1)).startswith(b"\x89PNG")
    assert ca.message.startswith("Exported the plan to")


def test_end_to_end_simulated_scan_coordinate_check_export(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Fast scan 470-700 MHz on the simulator, 10 generic analog mics in 470-694 MHz."""
    c, _pa, ca = env
    c.connect()
    run_until(c, lambda: c.state.connection == "connected")
    c.set_mode("scan")
    c.set_resolution(Resolution.FAST)
    c.set_range(470 * MHZ, 700 * MHZ)
    c.start()
    run_until(c, lambda: not c.state.busy, timeout=60)
    assert "Scan done" in c.state.message
    scan = c.resolve_trace("max")
    assert scan is not None and scan[1].start_hz <= 471 * MHZ and scan[1].stop_hz >= 699 * MHZ

    ca.model.add_device("UHF mic", 10)
    assert ca.coordinate()
    run_until(c, lambda: not ca.running, timeout=30)
    r = ca.result
    assert r is not None and r.plan.stats.complete, ca.message
    assert len(r.plan.assignments) == 10 and r.scan_label == "Max hold"
    assert all(470 * MHZ <= a.freq_hz <= 694 * MHZ for a in r.plan.assignments)
    assert all(a.scan_level_dbm is not None for a in r.plan.assignments)
    # Independently checked with solver.check(): no violations.
    ca.fill_check_from_result()
    assert ca.check()
    run_until(c, lambda: not ca.running)
    assert ca.check_outcome is not None and ca.check_outcome.report.ok
    for key in ("csv", "txt", "html"):
        assert ca.export(key, tmp_path / f"e2e.{key}")
    rows = list(csv.reader(io.StringIO((tmp_path / "e2e.csv").read_text())))
    assert sum(1 for row in rows[1:] if row[0].startswith("UHF mic #")) == 10
    assert "Complete: all 10 devices have a frequency" in (tmp_path / "e2e.txt").read_text()
    assert "UHF mic #10" in (tmp_path / "e2e.html").read_text()
