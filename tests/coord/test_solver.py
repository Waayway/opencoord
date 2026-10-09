"""Solver: candidate filtering, ranking, backtracking, groups, time budget, backups and check."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Sequence

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from opencoord.coord.channel_plans.model import BandAnnotation, ChannelPlan
from opencoord.coord.profiles import ChannelGroup, DeviceProfile, TuningRange
from opencoord.coord.solver import (
    MAX_DEVICES,
    Assignment,
    CoordinationRequest,
    LockedCarrier,
    Plan,
    check,
    solve,
)
from opencoord.coord.spacing import PartialSpacing, RunOverride, SpacingRules
from opencoord.core.types import ExclusionZone, Trace

KHZ = 1000
MHZ = 1_000_000
ANALOG = SpacingRules(350 * KHZ, 100 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ, 0)

log = logging.getLogger(__name__)


def tuned(
    name: str = "Mic",
    lo_mhz: float = 600.0,
    hi_mhz: float = 610.0,
    step_khz: int = 25,
    preset: str = "generic-analog",
    overrides: PartialSpacing | None = None,
) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        kind="mic",
        spacing_preset=preset,
        tuning=(TuningRange(round(lo_mhz * MHZ), round(hi_mhz * MHZ)),),
        step_hz=step_khz * KHZ,
        spacing_overrides=overrides or PartialSpacing(),
    )


def fixed(name: str, mhz: Sequence[float]) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        kind="mic",
        spacing_preset="generic-analog",
        channels=tuple(round(f * MHZ) for f in mhz),
    )


def grouped(name: str, groups: dict[str, Sequence[float]]) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        kind="mic",
        spacing_preset="generic-analog",
        groups=tuple(
            ChannelGroup(g, tuple(round(f * MHZ) for f in chans)) for g, chans in groups.items()
        ),
    )


def flat_trace(lo_mhz: float, hi_mhz: float, level: float = -100.0, step_khz: int = 10) -> Trace:
    freqs = np.arange(lo_mhz * MHZ, hi_mhz * MHZ + 1, step_khz * KHZ, dtype=np.float64)
    return Trace(freqs, np.full(freqs.size, level, dtype=np.float32), "scan")


def with_level(trace: Trace, lo_mhz: float, hi_mhz: float, level: float) -> Trace:
    dbm = trace.dbm.copy()
    dbm[(trace.freqs_hz >= lo_mhz * MHZ) & (trace.freqs_hz <= hi_mhz * MHZ)] = level
    return Trace(trace.freqs_hz, dbm, trace.label)


def assert_clean(plan: Plan, request: CoordinationRequest) -> None:
    report = check(plan.assignments, request)
    assert report.violations == (), report.violations


# ---------------------------------------------------------------- basics


def test_assigns_every_device_and_passes_check() -> None:
    req = CoordinationRequest(devices=[(tuned(), 4)])
    plan = solve(req)
    assert len(plan.assignments) == 4
    assert plan.unassigned == ()
    assert plan.stats.complete and not plan.stats.timed_out
    assert [a.label for a in plan.assignments] == ["Mic #1", "Mic #2", "Mic #3", "Mic #4"]
    assert all(a.profile_name == "Mic" for a in plan.assignments)
    freqs = [a.freq_hz for a in plan.assignments]
    assert freqs == sorted(freqs)
    assert all(600 * MHZ <= f <= 610 * MHZ and f % (25 * KHZ) == 0 for f in freqs)
    assert all(b - a >= 350 * KHZ for a, b in itertools.pairwise(freqs))
    assert_clean(plan, req)


def test_quantity_zero_is_ignored() -> None:
    plan = solve(CoordinationRequest(devices=[(tuned(), 0)]))
    assert plan.assignments == () and plan.unassigned == ()
    assert plan.stats.complete


def test_unknown_preset_raises() -> None:
    with pytest.raises(ValueError, match="nope"):
        solve(CoordinationRequest(devices=[(tuned(preset="nope"), 1)]))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold_db": -1.0},
        {"guard_hz": -1},
        {"time_budget_s": 0.0},
        {"backups_per_profile": -1},
    ],
)
def test_request_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CoordinationRequest(devices=[(tuned(), 1)], **kwargs)  # type: ignore[arg-type]


def test_negative_quantity_rejected() -> None:
    with pytest.raises(ValueError, match="quantity"):
        CoordinationRequest(devices=[(tuned(), -1)])


def test_duplicate_profile_names_rejected() -> None:
    with pytest.raises(ValueError, match="'Mic' is listed twice"):
        CoordinationRequest(devices=[(tuned(), 1), (tuned(lo_mhz=620, hi_mhz=630), 2)])


def test_total_quantity_is_capped() -> None:
    with pytest.raises(ValueError, match=str(MAX_DEVICES)):
        CoordinationRequest(devices=[(tuned(), MAX_DEVICES + 1)])


def test_max_devices_returns_without_recursion_error() -> None:
    profile = DeviceProfile(
        "D", "iem", "z", tuning=(TuningRange(470 * MHZ, 694 * MHZ),), step_hz=25 * KHZ
    )
    req = CoordinationRequest(
        devices=[(profile, MAX_DEVICES)],
        presets={"z": SpacingRules(carrier=50 * KHZ)},
        time_budget_s=2.0,
    )
    plan = solve(req)
    assert len(plan.assignments) + len(plan.unassigned) == MAX_DEVICES
    assert plan.stats.complete
    assert_clean(plan, req)


# ---------------------------------------------------------------- filtering and reasons


def test_no_candidates_reason() -> None:
    empty = DeviceProfile(name="Empty", kind="mic", spacing_preset="generic-analog")
    plan = solve(CoordinationRequest(devices=[(empty, 2), (tuned(), 1)]))
    assert [(u.label, u.reason) for u in plan.unassigned] == [
        ("Empty #1", "no-candidates-in-range"),
        ("Empty #2", "no-candidates-in-range"),
    ]
    assert len(plan.assignments) == 1
    assert not plan.stats.complete


def test_zones_exclude_candidates() -> None:
    zone = ExclusionZone(1, 600 * MHZ, 605 * MHZ)
    plan = solve(CoordinationRequest(devices=[(tuned(), 3)], zones=[zone]))
    assert all(a.freq_hz > 605 * MHZ for a in plan.assignments)
    everything = ExclusionZone(1, 599 * MHZ, 611 * MHZ)
    plan = solve(CoordinationRequest(devices=[(tuned(), 1)], zones=[everything]))
    assert [u.reason for u in plan.unassigned] == ["all-candidates-excluded"]


def eu_like() -> ChannelPlan:
    return ChannelPlan(
        name="test",
        title="Test",
        channels=(),
        bands=(BandAnnotation(600 * MHZ, 605 * MHZ, "forbidden", "Mobile band"),),
    )


def test_forbidden_bands_are_skipped_unless_allowed() -> None:
    profile = tuned(lo_mhz=600.0, hi_mhz=606.0)
    plan = solve(CoordinationRequest(devices=[(profile, 2)], channel_plan=eu_like()))
    assert all(a.freq_hz >= 605 * MHZ for a in plan.assignments)
    assert plan.warnings == ()

    only_forbidden = tuned(lo_mhz=600.0, hi_mhz=604.0)
    plan = solve(CoordinationRequest(devices=[(only_forbidden, 1)], channel_plan=eu_like()))
    assert [u.reason for u in plan.unassigned] == ["all-candidates-excluded"]

    plan = solve(
        CoordinationRequest(
            devices=[(only_forbidden, 2)], channel_plan=eu_like(), allow_forbidden=True
        )
    )
    assert len(plan.assignments) == 2
    assert len(plan.warnings) == 2
    assert all("forbidden" in w and "Mobile band" in w for w in plan.warnings)


def test_allowed_forbidden_band_is_used_after_legal_candidates() -> None:
    profile = tuned(lo_mhz=600.0, hi_mhz=606.0)
    plan = solve(
        CoordinationRequest(devices=[(profile, 1)], channel_plan=eu_like(), allow_forbidden=True)
    )
    assert plan.assignments[0].freq_hz >= 605 * MHZ
    assert plan.warnings == ()


def test_occupied_candidates_are_dropped() -> None:
    scan = with_level(flat_trace(590, 620), 600.0, 609.0, -40.0)
    plan = solve(CoordinationRequest(devices=[(tuned(), 2)], scan=scan))
    # guard 100 kHz (edges inclusive): 609.1 still sees the 609.0 bin; clean from 609.125
    assert all(a.freq_hz >= 609_125_000 for a in plan.assignments)
    assert len(plan.assignments) == 2

    scan = with_level(flat_trace(590, 620), 599.0, 611.0, -40.0)
    plan = solve(CoordinationRequest(devices=[(tuned(), 1)], scan=scan))
    assert [u.reason for u in plan.unassigned] == ["all-candidates-occupied"]

    # a higher threshold accepts the same candidates
    plan = solve(CoordinationRequest(devices=[(tuned(), 1)], scan=scan, threshold_db=80.0))
    assert len(plan.assignments) == 1


def test_guard_width_controls_occupancy() -> None:
    scan = with_level(flat_trace(590, 620), 605.0, 605.0, -40.0)
    profile = fixed("F", [604.5, 606.0])
    narrow = solve(CoordinationRequest(devices=[(profile, 2)], scan=scan, guard_hz=100 * KHZ))
    assert len(narrow.assignments) == 2
    wide = solve(CoordinationRequest(devices=[(profile, 2)], scan=scan, guard_hz=1100 * KHZ))
    assert [a.freq_hz for a in wide.assignments] == []
    assert [u.reason for u in wide.unassigned] == ["all-candidates-occupied"] * 2


def test_ranks_by_scan_level_quietest_first() -> None:
    scan = with_level(flat_trace(590, 620, -95.0), 607.0, 608.0, -105.0)
    plan = solve(CoordinationRequest(devices=[(tuned(), 1)], scan=scan))
    (a,) = plan.assignments
    assert 607 * MHZ + 100 * KHZ <= a.freq_hz <= 608 * MHZ - 100 * KHZ
    assert a.scan_level_dbm == pytest.approx(-105.0)


def test_unknown_level_ranks_after_known_quiet() -> None:
    scan = flat_trace(605, 620, -100.0)  # 600-605 is not covered by the scan
    plan = solve(CoordinationRequest(devices=[(tuned(), 1)], scan=scan))
    (a,) = plan.assignments
    assert a.freq_hz == 604_900_000  # first candidate whose guard window reaches the scan
    assert a.scan_level_dbm == pytest.approx(-100.0)

    plan = solve(CoordinationRequest(devices=[(tuned(), 1)]))
    assert plan.assignments[0].scan_level_dbm is None


def test_ranges_follow_the_candidate_source() -> None:
    # groups win over tuning in candidates(); the product window must cover them
    odd = DeviceProfile(
        "Odd",
        "mic",
        "generic-analog",
        tuning=(TuningRange(600 * MHZ, 601 * MHZ),),
        step_hz=25 * KHZ,
        groups=(ChannelGroup("A", (650 * MHZ, 652 * MHZ)),),
    )
    plan = solve(CoordinationRequest(devices=[(odd, 2)]))
    assert sorted(a.freq_hz for a in plan.assignments) == [650 * MHZ, 652 * MHZ]


# ---------------------------------------------------------------- locked carriers


def test_locked_carriers_take_part_in_checks() -> None:
    locked = [LockedCarrier(600 * MHZ, "IEM A"), LockedCarrier(601 * MHZ, "IEM B")]
    plan = solve(CoordinationRequest(devices=[(tuned(lo_mhz=599.0), 3)], locked=locked))
    assert len(plan.assignments) == 3
    freqs = [a.freq_hz for a in plan.assignments]
    # 2*600 - 601 = 599 and 2*601 - 600 = 602 are IM3 products of the locked pair
    assert all(abs(f - 599 * MHZ) >= 100 * KHZ for f in freqs)
    assert all(abs(f - 602 * MHZ) >= 100 * KHZ for f in freqs)
    assert all(abs(f - lf) >= 350 * KHZ for f in freqs for lf in (600 * MHZ, 601 * MHZ))
    assert_clean(plan, CoordinationRequest(devices=[(tuned(lo_mhz=599.0), 3)], locked=locked))


def test_locked_carrier_outside_tuning_ranges() -> None:
    locked = [LockedCarrier(800 * MHZ, "Far away")]
    req = CoordinationRequest(devices=[(tuned(), 2)], locked=locked)
    plan = solve(req)
    assert len(plan.assignments) == 2
    assert_clean(plan, req)


def test_locked_default_rules_are_generic_analog() -> None:
    assert LockedCarrier(600 * MHZ, "x").rules == ANALOG


def test_locked_clash_is_a_warning() -> None:
    locked = [LockedCarrier(600 * MHZ, "A"), LockedCarrier(600 * MHZ + 100 * KHZ, "B")]
    plan = solve(CoordinationRequest(devices=[(tuned(lo_mhz=620, hi_mhz=630), 1)], locked=locked))
    assert len(plan.assignments) == 1
    assert len(plan.warnings) == 1
    assert "A" in plan.warnings[0] and "B" in plan.warnings[0] and "carrier" in plan.warnings[0]


# ---------------------------------------------------------------- IMD conflicts and search


def test_imd_conflicts_reason_and_explanation() -> None:
    profile = fixed("Cheap", [800.0, 800.2, 800.4])
    plan = solve(CoordinationRequest(devices=[(profile, 3)]))
    assert sorted(a.freq_hz for a in plan.assignments) == [800 * MHZ, 800_400_000]
    (u,) = plan.unassigned
    assert u.label == "Cheap #3" and u.reason == "imd-conflicts"
    assert u.blocked_by is not None
    assert u.blocked_by.rule == "carrier"
    assert u.blocked_by.required_hz == 350 * KHZ
    assert u.blocked_by.actual_hz == 200 * KHZ
    assert plan.stats.complete is False and plan.stats.timed_out is False


def test_finds_complete_plan_on_a_fixed_set() -> None:
    profile = fixed("F", [800.0, 800.2, 800.4, 801.0, 802.5])
    req = CoordinationRequest(devices=[(profile, 3)])
    plan = solve(req)
    assert plan.stats.complete
    assert_clean(plan, req)


def brute_max(profile: DeviceProfile, qty: int, req: CoordinationRequest) -> int:
    chans = list(profile.channels)
    for k in range(min(qty, len(chans)), 0, -1):
        for combo in itertools.combinations(chans, k):
            hand = [
                Assignment(f"{profile.name} #{i + 1}", profile.name, f) for i, f in enumerate(combo)
            ]
            if not check(hand, req).violations:
                return k
    return 0


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    offsets=st.lists(st.integers(0, 120), min_size=1, max_size=7, unique=True),
    qty=st.integers(1, 6),
)
def test_exhaustive_search_finds_the_maximum(offsets: list[int], qty: int) -> None:
    profile = fixed("F", [800 + o * 0.05 for o in sorted(offsets)])
    req = CoordinationRequest(devices=[(profile, qty)], time_budget_s=30.0)
    plan = solve(req)
    assert not plan.stats.timed_out
    assert len(plan.assignments) == brute_max(profile, qty, req)
    assert_clean(plan, req)


def test_most_constrained_profile_is_placed_first() -> None:
    # The fixed set has one usable channel; placing the wide-range mic first could block it.
    tight = fixed("Tight", [605.0])
    req = CoordinationRequest(devices=[(tuned(lo_mhz=604.0, hi_mhz=606.0), 2), (tight, 1)])
    plan = solve(req)
    assert plan.stats.complete
    assert any(a.profile_name == "Tight" and a.freq_hz == 605 * MHZ for a in plan.assignments)


def test_run_override_scales_spacing() -> None:
    req = CoordinationRequest(devices=[(fixed("F", [600.0, 600.5, 601.5]), 2)])
    assert len(solve(req).assignments) == 2
    wide = CoordinationRequest(
        devices=[(fixed("F", [600.0, 600.5, 601.5]), 2)], run_override=RunOverride(scale=3.0)
    )
    plan = solve(wide)
    assert sorted(a.freq_hz for a in plan.assignments) == [600 * MHZ, 601_500_000]


def test_profile_overrides_and_custom_presets() -> None:
    profile = fixed("F", [600.0, 600.1])
    custom = {"tight": SpacingRules(carrier=50 * KHZ)}
    req = CoordinationRequest(
        devices=[(DeviceProfile("F", "mic", "tight", channels=profile.channels), 2)],
        presets=custom,
    )
    assert len(solve(req).assignments) == 2
    strict = DeviceProfile(
        "F", "mic", "tight", channels=profile.channels, spacing_overrides=PartialSpacing(200 * KHZ)
    )
    assert len(solve(CoordinationRequest(devices=[(strict, 2)], presets=custom)).assignments) == 1


def test_stricter_rule_of_two_profiles_applies() -> None:
    presets = {"loose": SpacingRules(carrier=50 * KHZ), "strict": SpacingRules(carrier=500 * KHZ)}
    a = DeviceProfile("A", "mic", "loose", channels=(600 * MHZ,))
    b = DeviceProfile("B", "mic", "strict", channels=(600_200_000,))
    plan = solve(CoordinationRequest(devices=[(a, 1), (b, 1)], presets=presets))
    assert len(plan.assignments) == 1


# ---------------------------------------------------------------- groups


def test_single_group_preferred() -> None:
    profile = grouped("Set", {"A": [610.0, 610.1, 611.0], "B": [620.0, 621.0, 623.5]})
    req = CoordinationRequest(devices=[(profile, 3)])
    plan = solve(req)
    assert plan.stats.complete
    assert {a.group for a in plan.assignments} == {"B"}
    assert_clean(plan, req)


def test_groups_ordered_by_capacity() -> None:
    profile = grouped("Set", {"Small": [630.0], "Big": [610.0, 611.0, 613.5]})
    plan = solve(CoordinationRequest(devices=[(profile, 1)]))
    assert plan.assignments[0].group == "Big"


def test_falls_back_to_mixing_groups() -> None:
    profile = grouped("Set", {"A": [600.0, 600.1, 603.0], "B": [610.0, 610.1, 617.0]})
    req = CoordinationRequest(devices=[(profile, 3)])
    plan = solve(req)
    assert plan.stats.complete
    assert len({a.group for a in plan.assignments}) == 2
    assert_clean(plan, req)


def test_mixing_when_single_group_preference_is_off() -> None:
    profile = grouped("Set", {"A": [600.0, 640.0], "B": [610.0, 620.0, 623.5]})
    plan = solve(CoordinationRequest(devices=[(profile, 2)], prefer_single_group=False))
    # quietest-first by frequency without a scan: 600 (A) then 610 (B)
    assert [a.group for a in plan.assignments] == ["A", "B"]
    plan = solve(CoordinationRequest(devices=[(profile, 2)]))
    assert {a.group for a in plan.assignments} == {"B"}


# ---------------------------------------------------------------- time budget


class FakeClock:
    def __init__(self, step: float) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_time_budget_returns_best_partial() -> None:
    req = CoordinationRequest(devices=[(tuned(), 8)], time_budget_s=4.0, clock=FakeClock(1.0))
    plan = solve(req)
    assert plan.stats.timed_out
    assert not plan.stats.complete
    assert 0 < len(plan.assignments) < 8
    assert all(u.reason == "time-budget" for u in plan.unassigned)
    assert len(plan.assignments) + len(plan.unassigned) == 8
    assert_clean(plan, req)


def test_blocked_from_the_start_is_imd_conflicts_even_on_timeout() -> None:
    # every candidate of "Blocked" sits on the locked carrier; the search itself times out
    blocked = fixed("Blocked", [605.0])
    req = CoordinationRequest(
        devices=[(tuned(), 8), (blocked, 1)],
        locked=[LockedCarrier(605 * MHZ, "L")],
        time_budget_s=4.0,
        clock=FakeClock(1.0),
    )
    plan = solve(req)
    assert plan.stats.timed_out
    reasons = {u.label: u.reason for u in plan.unassigned}
    assert reasons["Blocked #1"] == "imd-conflicts"
    assert any(r == "time-budget" for lb, r in reasons.items() if lb.startswith("Mic"))


# ---------------------------------------------------------------- output details


def test_backups_are_compatible_with_the_whole_plan() -> None:
    req = CoordinationRequest(devices=[(tuned(), 3), (fixed("F", [604.0, 606.0, 608.0]), 1)])
    plan = solve(req)
    assert plan.stats.complete
    assert set(plan.backups) == {"Mic", "F"}
    assert len(plan.backups["Mic"]) == 2
    used = {a.freq_hz for a in plan.assignments}
    assert not used & {f for fs in plan.backups.values() for f in fs}
    assert_jointly_clean(plan, req)

    none = solve(CoordinationRequest(devices=[(tuned(), 1)], backups_per_profile=0))
    assert none.backups == {"Mic": ()}
    with pytest.raises(TypeError):
        none.backups["Mic"] = (1,)  # type: ignore[index]


def assert_jointly_clean(plan: Plan, req: CoordinationRequest) -> None:
    spares = [
        Assignment(f"{name} backup {k + 1}", name, f)
        for name, fs in plan.backups.items()
        for k, f in enumerate(fs)
    ]
    assert len({a.freq_hz for a in spares}) == len(spares)
    assert check([*plan.assignments, *spares], req).violations == ()


def test_backups_are_mutually_compatible() -> None:
    # review case: four backups used to cluster within one guard width of each other
    req = CoordinationRequest(devices=[(tuned(), 3)], backups_per_profile=4)
    plan = solve(req)
    assert len(plan.backups["Mic"]) == 4
    assert_jointly_clean(plan, req)
    # two profiles over the same range: backups neither coincide nor clash across profiles
    req = CoordinationRequest(
        devices=[(tuned(), 2), (tuned("Other", 600.0, 610.0), 2)], backups_per_profile=3
    )
    plan = solve(req)
    assert len(plan.backups["Mic"]) == 3 and len(plan.backups["Other"]) == 3
    assert_jointly_clean(plan, req)


def test_nearest_imd_distance() -> None:
    plan = solve(CoordinationRequest(devices=[(fixed("F", [600.0, 601.0, 603.5]), 3)]))
    by_f = {a.freq_hz: a for a in plan.assignments}
    # products of the other two carriers: for 603.5: 2*601-600 = 602 (1.5 MHz), 3*601-2*600 = 603
    assert by_f[603_500_000].nearest_imd_margin_hz == 500 * KHZ
    alone = solve(CoordinationRequest(devices=[(tuned(), 1)]))
    assert alone.assignments[0].nearest_imd_margin_hz is None


def test_deterministic() -> None:
    scan = with_level(flat_trace(590, 620, -95.0), 603.0, 604.0, -100.0)
    req = CoordinationRequest(
        devices=[(tuned(), 5), (fixed("F", [601.0, 605.0, 609.0]), 2)],
        scan=scan,
        locked=[LockedCarrier(607 * MHZ, "L")],
    )
    a, b = solve(req), solve(req)
    assert (a.assignments, a.unassigned, a.backups, a.warnings) == (
        b.assignments,
        b.unassigned,
        b.backups,
        b.warnings,
    )
    assert a.stats.nodes == b.stats.nodes


# ---------------------------------------------------------------- check


def test_check_reports_every_violation() -> None:
    req = CoordinationRequest(devices=[(tuned(), 3)])
    hand = [
        Assignment("Mic #1", "Mic", 600 * MHZ),
        Assignment("Mic #2", "Mic", 600_200_000),
        Assignment("Mic #3", "Mic", 600_400_000),
    ]
    report = check(hand, req)
    rules = sorted((v.rule, v.sources, v.victim) for v in report.violations)
    assert ("carrier", ("Mic #1", "Mic #2"), None) in rules
    assert ("carrier", ("Mic #2", "Mic #3"), None) in rules
    carrier = [v for v in report.violations if v.rule == "carrier" and "Mic #3" in v.sources]
    assert carrier[0].required_hz == 350 * KHZ
    # 2*600.2 - 600.0 = 600.4 lands exactly on Mic #3
    im3 = [v for v in report.violations if v.rule == "im3_2tx" and v.victim == "Mic #3"]
    assert im3 and im3[0].actual_hz == 0 and im3[0].required_hz == 100 * KHZ
    assert set(im3[0].sources) == {"Mic #1", "Mic #2"}
    assert im3[0].product_hz == 600_400_000
    assert not report.ok


def test_check_clean_plan_and_warnings() -> None:
    zone = ExclusionZone(1, 609 * MHZ, 610 * MHZ)
    scan = with_level(flat_trace(590, 620), 605.0, 605.0, -40.0)
    req = CoordinationRequest(
        devices=[(tuned(), 3)], zones=[zone], scan=scan, channel_plan=eu_like()
    )
    hand = [
        Assignment("Mic #1", "Mic", 602 * MHZ),
        Assignment("Mic #2", "Mic", 605 * MHZ),
        Assignment("Mic #3", "Mic", 609_500_000),
    ]
    report = check(hand, req)
    assert report.ok and report.violations == ()
    text = " | ".join(report.warnings)
    assert "Mic #1" in text and "forbidden" in text
    assert "Mic #2" in text and "occupied" in text
    assert "Mic #3" in text and "exclusion zone" in text


def test_check_off_grid_frequency_warns() -> None:
    req = CoordinationRequest(devices=[(tuned(), 1)])
    report = check([Assignment("Mic #1", "Mic", 650 * MHZ)], req)
    assert report.violations == ()
    assert any("not a candidate" in w for w in report.warnings)


def test_check_locked_only_clash_is_a_warning() -> None:
    locked = [LockedCarrier(600 * MHZ, "A"), LockedCarrier(600_100_000, "B")]
    req = CoordinationRequest(devices=[(tuned(), 1)], locked=locked)
    report = check([Assignment("Mic #1", "Mic", 605 * MHZ)], req)
    assert report.violations == ()
    assert any("A" in w and "B" in w for w in report.warnings)
    report = check([Assignment("Mic #1", "Mic", 600_200_000)], req)
    assert any(v.rule == "carrier" and "Mic #1" in v.sources for v in report.violations)


def test_check_rejects_unknown_profile_and_duplicate_labels() -> None:
    req = CoordinationRequest(devices=[(tuned(), 1)])
    with pytest.raises(ValueError, match="Other"):
        check([Assignment("X", "Other", 600 * MHZ)], req)
    with pytest.raises(ValueError, match="Mic #1"):
        check([Assignment("Mic #1", "Mic", 600 * MHZ), Assignment("Mic #1", "Mic", 605 * MHZ)], req)


# ---------------------------------------------------------------- invariant (Hypothesis)

RULES = st.builds(
    SpacingRules,
    carrier=st.sampled_from([0, 100 * KHZ, 350 * KHZ]),
    im3_2tx=st.sampled_from([0, 50 * KHZ, 100 * KHZ]),
    im3_3tx=st.sampled_from([0, 50 * KHZ]),
    im5_2tx=st.sampled_from([0, 50 * KHZ]),
    im7_2tx=st.sampled_from([0, 50 * KHZ]),
    im5_3tx=st.sampled_from([0, 0, 50 * KHZ]),
)


@st.composite
def requests(draw: st.DrawFn) -> CoordinationRequest:
    presets = {f"p{i}": draw(RULES) for i in range(2)}
    devices: list[tuple[DeviceProfile, int]] = []
    for i in range(draw(st.integers(1, 3))):
        preset = draw(st.sampled_from(sorted(presets)))
        kind = draw(st.sampled_from(["tuning", "channels", "groups"]))
        name = f"P{i}"
        if kind == "tuning":
            lo = draw(st.integers(600, 640)) * 250 * KHZ
            span = draw(st.integers(1, 12)) * 250 * KHZ
            profile = DeviceProfile(
                name, "mic", preset, tuning=(TuningRange(lo, lo + span),), step_hz=125 * KHZ
            )
        else:
            chans = draw(
                st.lists(st.integers(1200, 1300), min_size=1, max_size=8, unique=True).map(
                    lambda xs: tuple(x * 125 * KHZ for x in xs)
                )
            )
            if kind == "channels":
                profile = DeviceProfile(name, "mic", preset, channels=chans)
            else:
                cut = draw(st.integers(0, len(chans)))
                parts = [p for p in (chans[:cut], chans[cut:]) if p]
                groups = tuple(ChannelGroup(f"G{j}", p) for j, p in enumerate(parts))
                profile = DeviceProfile(name, "mic", preset, groups=groups)
        devices.append((profile, draw(st.integers(0, 4))))
    locked = [
        LockedCarrier(draw(st.integers(1150, 1350)) * 125 * KHZ, f"L{i}", draw(RULES))
        for i in range(draw(st.integers(0, 2)))
    ]
    zones = [
        ExclusionZone(1, z * 125 * KHZ, z * 125 * KHZ + 500 * KHZ)
        for z in draw(st.lists(st.integers(1200, 1300), max_size=1))
    ]
    scan = None
    if draw(st.booleans()):
        scan = with_level(flat_trace(140, 170, -100.0, step_khz=50), 155.0, 156.0, -50.0)
    return CoordinationRequest(
        devices=devices,
        locked=locked,
        zones=zones,
        scan=scan,
        presets=presets,
        prefer_single_group=draw(st.booleans()),
        run_override=draw(st.sampled_from([None, RunOverride(scale=1.5)])),
        time_budget_s=0.5,
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(req=requests())
def test_no_returned_plan_violates_any_rule(req: CoordinationRequest) -> None:
    plan = solve(req)
    report = check(plan.assignments, req)
    assert report.violations == ()
    total = sum(q for _, q in req.devices)
    assert len(plan.assignments) + len(plan.unassigned) == total
    assert plan.stats.complete == (len(plan.unassigned) == 0)
    for a in plan.assignments:
        assert not any(z.start_hz <= a.freq_hz <= z.stop_hz for z in req.zones)
    assert_jointly_clean(plan, req)


# ---------------------------------------------------------------- performance

UHF = tuned("Analog", 470.0, 694.0)


def test_performance_16_devices() -> None:
    req = CoordinationRequest(devices=[(UHF, 16)])
    t0 = time.perf_counter()
    plan = solve(req)
    elapsed = time.perf_counter() - t0
    log.info("16 devices 470-694 MHz: %.3f s, %d nodes", elapsed, plan.stats.nodes)
    assert plan.stats.complete
    assert elapsed < 5.0
    assert_clean(plan, req)


def test_performance_40_devices_within_budget() -> None:
    req = CoordinationRequest(devices=[(UHF, 40)], time_budget_s=5.0)
    t0 = time.perf_counter()
    plan = solve(req)
    elapsed = time.perf_counter() - t0
    log.info(
        "40 devices 470-694 MHz: %.3f s, %d nodes, %d assigned",
        elapsed,
        plan.stats.nodes,
        len(plan.assignments),
    )
    assert elapsed < 5.0
    assert plan.stats.complete
    assert_clean(plan, req)
