"""Coordination panel state (no Dear PyGui): setup editing, request building, check input,
setup and result serialisation."""

from __future__ import annotations

import json
import types

import numpy as np
import pytest

from opencoord.coord.channel_plans import load as load_plan
from opencoord.coord.profiles import DeviceProfile, TuningRange
from opencoord.coord.solver import (
    Assignment,
    Plan,
    SolveStats,
    Unassigned,
    Violation,
    check,
    solve,
)
from opencoord.coord.spacing import PartialSpacing, RunOverride, SpacingRules, builtin_presets
from opencoord.core.types import ExclusionZone, Trace
from opencoord.ui.coordination_model import (
    MAX_DEVICE_ROWS,
    MAX_LOCKS,
    CoordinationModel,
    CoordinationResult,
    LockRow,
    build_request,
    check_input,
    result_from_dict,
    result_to_dict,
)

MHZ = 1_000_000
KHZ = 1000
PRESETS = {name: p.rules for name, p in builtin_presets().items()}


def profile(name: str = "Mic", lo: float = 600.0, hi: float = 610.0) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        kind="mic",
        spacing_preset="generic-analog",
        tuning=(TuningRange(round(lo * MHZ), round(hi * MHZ)),),
        step_hz=25 * KHZ,
    )


PROFILES = {"Mic": profile(), "IEM": profile("IEM", 620.0, 630.0)}


def request_for(m: CoordinationModel, scan: Trace | None = None):  # type: ignore[no-untyped-def]
    return build_request(
        m,
        profiles=PROFILES,
        presets=PRESETS,
        scan=scan,
        zones=[ExclusionZone(1, 601 * MHZ, 602 * MHZ)],
        channel_plan=load_plan("eu"),
    )


# --- editing ----------------------------------------------------------------------------------


def test_device_rows_are_edited_and_bounded() -> None:
    m = CoordinationModel()
    s0, r0 = m.structure_version, m.revision
    assert m.add_device("Mic", 4) == 0
    assert m.structure_version > s0 and m.revision > r0
    m.set_quantity(0, -3)
    assert m.rows[0].quantity == 0
    m.set_quantity(0, 10_000)
    assert m.rows[0].quantity == 200
    m.set_device_profile(0, "IEM")
    assert m.rows[0].profile == "IEM"
    for _ in range(MAX_DEVICE_ROWS - 1):
        assert m.add_device("Mic") is not None
    assert m.add_device("Mic") is None
    s1 = m.structure_version
    m.remove_device(0)
    assert len(m.rows) == MAX_DEVICE_ROWS - 1 and m.structure_version > s1
    m.remove_device(99)  # unknown index: ignored
    m.set_quantity(99, 1)


def test_paste_locks_uses_the_channel_parser() -> None:
    m = CoordinationModel()
    res = m.paste_locks("606.5,606.1\n470,125 junk", label="TV link", preset="iem")
    assert res.errors and "'junk' is not a number" in res.errors[0]
    assert [lk.freq_hz for lk in m.locks] == [470_125_000, 606_100_000, 606_500_000]
    assert [lk.label for lk in m.locks] == ["TV link 1", "TV link 2", "TV link 3"]
    assert {lk.preset for lk in m.locks} == {"iem"}
    # One value with a label keeps the label as is; no label gives a frequency label.
    m.clear_locks()
    m.paste_locks("610", label="Venue")
    m.paste_locks("611.25")
    assert [lk.label for lk in m.locks] == ["Venue", "Locked 611.25"]
    # Values already locked are skipped.
    res = m.paste_locks("610 612")
    assert [lk.freq_hz for lk in m.locks] == [610 * MHZ, 611_250_000, 612 * MHZ]
    m.set_lock_label(0, "  ")
    assert m.locks[0].label == "Locked 610"
    m.set_lock_preset(0, "digital")
    assert m.locks[0] == LockRow(610 * MHZ, "Locked 610", "digital")
    m.remove_lock(1)
    assert len(m.locks) == 2


def test_paste_locks_stops_at_the_limit() -> None:
    m = CoordinationModel()
    text = " ".join(str(500 + i) for i in range(MAX_LOCKS + 3))
    res = m.paste_locks(text)
    assert len(m.locks) == MAX_LOCKS
    assert any(f"at most {MAX_LOCKS}" in e for e in res.errors)


def test_options_are_clamped() -> None:
    m = CoordinationModel()
    m.set_threshold_db(-5)
    m.set_guard_khz(-1)
    m.set_time_budget_s(0)
    m.set_backups(99)
    m.set_override_scale(0)
    o = m.options
    assert o.threshold_db == 0 and o.guard_khz == 0
    assert o.time_budget_s == 0.5 and o.backups_per_profile == 10
    assert o.override_scale == 0.1
    m.set_time_budget_s(float("nan"))
    assert m.options.time_budget_s == 0.5
    m.set_override_value("im3_2tx", 150.0)
    m.set_override_value("carrier", None)
    assert m.options.override_khz == {"im3_2tx": 150.0}
    with pytest.raises(KeyError):
        m.set_override_value("bogus", 1.0)


# --- requests ---------------------------------------------------------------------------------


def test_build_request_maps_every_input() -> None:
    m = CoordinationModel()
    m.add_device("Mic", 3)
    m.add_device("IEM", 2)
    m.add_device("Mic", 1)  # same profile twice: quantities are merged
    m.paste_locks("615", label="Venue", preset="iem")
    m.set_threshold_db(12)
    m.set_guard_khz(50)
    m.set_allow_forbidden(True)
    m.set_prefer_single_group(False)
    m.set_time_budget_s(2)
    m.set_backups(1)
    m.set_override_enabled(True)
    m.set_override_scale(1.5)
    m.set_override_value("im3_2tx", 150)
    scan = Trace(np.array([600e6, 610e6]), np.array([-100, -90], dtype=np.float32), "Max hold")
    req = request_for(m, scan)
    assert [(p.name, q) for p, q in req.devices] == [("Mic", 4), ("IEM", 2)]
    assert req.locked[0].freq_hz == 615 * MHZ and req.locked[0].label == "Venue"
    assert req.locked[0].rules == PRESETS["iem"]
    assert req.scan is scan and req.zones == (ExclusionZone(1, 601 * MHZ, 602 * MHZ),)
    assert req.channel_plan is not None and req.channel_plan.name == "eu"
    assert (req.threshold_db, req.guard_hz, req.allow_forbidden) == (12, 50 * KHZ, True)
    assert (req.prefer_single_group, req.time_budget_s, req.backups_per_profile) == (False, 2, 1)
    assert req.run_override == RunOverride(1.5, PartialSpacing(im3_2tx=150 * KHZ))
    assert dict(req.presets) == PRESETS
    # The override is kept but not used while switched off; without "use scan" no scan.
    m.set_override_enabled(False)
    m.set_use_scan(False)
    req = request_for(m, scan)
    assert req.run_override is None and req.scan is None


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda m: None, "Add at least one device"),
        (lambda m: m.add_device("Mic", 0), "Add at least one device"),
        (lambda m: m.add_device("", 1), "Choose a profile for device row 1"),
        (lambda m: m.add_device("Gone", 1), "Profile 'Gone' is not loaded"),
        (
            lambda m: (m.add_device("Mic", 150), m.add_device("IEM", 60)),
            "at most 200 devices",
        ),
        (
            lambda m: (m.add_device("Mic"), m.paste_locks("615", preset="nope")),
            "unknown spacing preset 'nope'",
        ),
    ],
)
def test_build_request_errors_are_readable(setup, message: str) -> None:  # type: ignore[no-untyped-def]
    m = CoordinationModel()
    setup(m)
    with pytest.raises(ValueError, match=message):
        request_for(m)


def test_check_input_labels_per_profile_and_validates() -> None:
    m = CoordinationModel()
    m.add_device("Mic", 2)
    m.add_device("IEM", 1)
    m.add_device("Mic", 1)
    with pytest.raises(ValueError, match="Type or paste frequencies"):
        check_input(m, profiles=PROFILES, presets=PRESETS, scan=None, zones=(), channel_plan=None)
    m.set_check_text(0, "600.2; 600.000")
    m.set_check_text(2, "604")
    m.set_check_text(1, "625")
    req, assignments = check_input(
        m, profiles=PROFILES, presets=PRESETS, scan=None, zones=(), channel_plan=None
    )
    assert [(a.label, a.profile_name, a.freq_hz) for a in assignments] == [
        ("Mic #1", "Mic", 600 * MHZ),
        ("Mic #2", "Mic", 600_200_000),
        ("IEM #1", "IEM", 625 * MHZ),
        ("Mic #3", "Mic", 604 * MHZ),
    ]
    assert [(p.name, q) for p, q in req.devices] == [("Mic", 3), ("IEM", 1)]
    report = check(assignments, req)
    assert any(v.rule == "carrier" for v in report.violations)  # 600.0 / 600.2 too close
    m.set_check_text(1, "625 oops")
    with pytest.raises(ValueError, match=r"IEM \(row 2\): 'oops' is not a number"):
        check_input(m, profiles=PROFILES, presets=PRESETS, scan=None, zones=(), channel_plan=None)


def test_fill_check_from_a_plan_splits_by_row() -> None:
    m = CoordinationModel()
    m.add_device("Mic", 2)
    m.add_device("IEM", 1)
    m.add_device("Mic", 1)
    m.add_device("Gone", 1)
    m.set_check_text(3, "old")
    plan = solve(request_for(model_with(("Mic", 3), ("IEM", 1))))
    assert plan.stats.complete
    m.fill_check(plan)
    mic = sorted(a.freq_hz for a in plan.assignments if a.profile_name == "Mic")
    assert m.rows[0].check_text.count(";") == 1 and m.rows[2].check_text.count(";") == 0
    assert m.rows[2].check_text and m.rows[1].check_text
    _, assignments = check_input(
        m, profiles=PROFILES, presets=PRESETS, scan=None, zones=(), channel_plan=None
    )
    assert sorted(a.freq_hz for a in assignments if a.profile_name == "Mic") == mic
    assert m.rows[3].check_text == ""


def model_with(*rows: tuple[str, int]) -> CoordinationModel:
    m = CoordinationModel()
    for name, q in rows:
        m.add_device(name, q)
    return m


# --- persistence ------------------------------------------------------------------------------


def test_setup_round_trips_through_json() -> None:
    m = CoordinationModel()
    m.add_device("Mic", 3)
    m.set_check_text(0, "600.1; 600.5")
    m.paste_locks("615", label="Venue", preset="iem")
    m.set_threshold_db(7.5)
    m.set_guard_khz(60)
    m.set_allow_forbidden(True)
    m.set_prefer_single_group(False)
    m.set_time_budget_s(3)
    m.set_backups(4)
    m.set_use_scan(False)
    m.set_override_enabled(True)
    m.set_override_scale(0.8)
    m.set_override_value("carrier", 300)
    data = json.loads(json.dumps(m.to_dict()))
    m2 = CoordinationModel.from_dict(data)
    assert m2.rows == m.rows and m2.locks == m.locks and m2.options == m.options


def test_setup_from_dict_tolerates_missing_and_rejects_bad_types() -> None:
    m = CoordinationModel.from_dict({})
    assert m.rows == [] and m.locks == []
    with pytest.raises(ValueError):
        CoordinationModel.from_dict({"devices": [{"profile": 3, "quantity": 1}]})
    with pytest.raises(ValueError):
        CoordinationModel.from_dict({"locked": [{"freq_hz": "x", "label": "a"}]})
    with pytest.raises(ValueError):
        CoordinationModel.from_dict({"options": {"override": {"values_khz": {"bogus": 1}}}})
    # Out-of-range numbers are clamped like the setters do.
    m = CoordinationModel.from_dict({"options": {"threshold_db": -4, "backups_per_profile": 50}})
    assert m.options.threshold_db == 0 and m.options.backups_per_profile == 10


def sample_result() -> CoordinationResult:
    blocker = Violation("im3_2tx", 100 * KHZ, 25 * KHZ, ("Mic #1", "Mic #2"), "Mic #4", 600 * MHZ)
    plan = Plan(
        (
            Assignment("Mic #1", "Mic", 600_125_000, "A", -98.5, 400_000),
            Assignment("Mic #2", "Mic", 600_500_000, None, None, None),
        ),
        (
            Unassigned("Mic #3", "Mic", "imd-conflicts", blocker),
            Unassigned("Mic #4", "Mic", "time-budget"),
        ),
        types.MappingProxyType({"Mic": (601 * MHZ,)}),
        ("careful",),
        SolveStats(0.25, 42, False, True),
    )
    return CoordinationResult(
        plan, (LockRow(615 * MHZ, "Venue", "iem"),), "Max hold", "2026-10-09T10:00:00+00:00"
    )


def test_result_round_trips_through_json() -> None:
    r = sample_result()
    back = result_from_dict(json.loads(json.dumps(result_to_dict(r))))
    assert back.plan.assignments == r.plan.assignments
    assert back.plan.unassigned == r.plan.unassigned
    assert dict(back.plan.backups) == {"Mic": (601 * MHZ,)}
    assert isinstance(back.plan.backups, types.MappingProxyType)
    assert back.plan.warnings == r.plan.warnings and back.plan.stats == r.plan.stats
    assert (back.locked, back.scan_label, back.created) == (r.locked, r.scan_label, r.created)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("assignments"),
        lambda d: d["assignments"][0].update(freq_hz="600"),
        lambda d: d["unassigned"][0].update(reason="bored"),
        lambda d: d["backups"].update(Mic=["x"]),
        lambda d: d["stats"].update(nodes=None),
    ],
)
def test_result_from_dict_rejects_bad_data(mutate) -> None:  # type: ignore[no-untyped-def]
    d = json.loads(json.dumps(result_to_dict(sample_result())))
    mutate(d)
    with pytest.raises(ValueError):
        result_from_dict(d)


def test_rules_of_locks_default_to_generic_analog() -> None:
    m = CoordinationModel()
    m.add_device("Mic")
    m.paste_locks("615")
    assert m.locks[0].preset == "generic-analog"
    assert request_for(m).locked[0].rules == SpacingRules(
        350 * KHZ, 100 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ, 0
    )
