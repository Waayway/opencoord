"""Spacing rules: parsing, serialising, resolution."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.coord.spacing import (
    PartialSpacing,
    RunOverride,
    SpacingError,
    SpacingPreset,
    SpacingRules,
    builtin_presets,
    parse_preset,
    preset_to_toml,
    resolve_profile_rules,
)

KHZ = 1000

hz = st.integers(min_value=0, max_value=5_000_000_000)
rules_st = st.builds(SpacingRules, hz, hz, hz, hz, hz, hz)


def test_builtin_presets_have_the_decided_values() -> None:
    p = builtin_presets()
    assert sorted(p) == ["conservative", "digital", "generic-analog", "iem"]
    assert p["generic-analog"].rules == SpacingRules(
        350 * KHZ, 100 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ, 0
    )
    assert p["conservative"].rules == SpacingRules(
        400 * KHZ, 150 * KHZ, 75 * KHZ, 75 * KHZ, 75 * KHZ, 0
    )
    assert p["iem"].rules == SpacingRules(600 * KHZ, 200 * KHZ, 100 * KHZ, 100 * KHZ, 100 * KHZ, 0)
    assert p["digital"].rules == SpacingRules(200 * KHZ, 50 * KHZ, 25 * KHZ, 25 * KHZ, 25 * KHZ, 0)


def test_parse_preset_reads_khz_and_defaults_missing_to_disabled() -> None:
    preset = parse_preset(
        '[preset]\nname = "x"\ndescription = "d"\n[spacing]\ncarrier = 12.5\nim3_2tx = 100\n'
    )
    assert preset.name == "x"
    assert preset.description == "d"
    assert preset.rules.carrier == 12_500
    assert preset.rules.im3_2tx == 100_000
    assert preset.rules.im7_2tx == 0


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("not toml [", "TOML"),
        ("[spacing]\ncarrier = 1\n", "name"),
        ('[preset]\nname = "x"\n[spacing]\ncarrier = -5\n', "carrier"),
        ('[preset]\nname = "x"\n[spacing]\ncarrier = "wide"\n', "carrier"),
        ('[preset]\nname = "x"\n[spacing]\nim9_2tx = 5\n', "im9_2tx"),
        ('[preset]\nname = "  "\n[spacing]\ncarrier = 5\n', "name"),
    ],
)
def test_parse_preset_errors_name_the_field(text: str, needle: str) -> None:
    with pytest.raises(SpacingError, match=needle):
        parse_preset(text)


def test_resolve_takes_the_stricter_value_per_field() -> None:
    a = SpacingRules(100, 0, 30, 5, 5, 0)
    b = SpacingRules(50, 20, 10, 5, 0, 7)
    assert a.resolve(b) == SpacingRules(100, 20, 30, 5, 5, 7)


@given(rules_st, rules_st)
def test_resolve_is_commutative_and_at_least_both(a: SpacingRules, b: SpacingRules) -> None:
    r = a.resolve(b)
    assert r == b.resolve(a)
    for f in SpacingRules.FIELDS:
        assert getattr(r, f) >= getattr(a, f)
        assert getattr(r, f) >= getattr(b, f)


@given(rules_st)
def test_preset_serialise_parse_round_trip(rules: SpacingRules) -> None:
    preset = SpacingPreset("my preset", "some text", rules)
    assert parse_preset(preset_to_toml(preset)) == preset


def test_resolution_order_run_then_profile_then_preset() -> None:
    preset = SpacingRules(350_000, 100_000, 50_000, 50_000, 50_000, 0)
    profile = PartialSpacing(im3_2tx=0, im5_3tx=25_000)
    run = RunOverride(values=PartialSpacing(carrier=500_000))
    out = resolve_profile_rules(preset, profile, run)
    assert out == SpacingRules(500_000, 0, 50_000, 50_000, 50_000, 25_000)


def test_run_scale_applies_after_all_overrides() -> None:
    preset = SpacingRules(400_000, 100_000, 0, 50_000, 50_000, 0)
    run = RunOverride(scale=0.5, values=PartialSpacing(im7_2tx=200_000))
    out = resolve_profile_rules(preset, PartialSpacing(), run)
    assert out == SpacingRules(200_000, 50_000, 0, 25_000, 100_000, 0)


def test_no_run_override_is_identity() -> None:
    preset = SpacingRules(1, 2, 3, 4, 5, 6)
    assert resolve_profile_rules(preset, PartialSpacing(), None) == preset
    assert resolve_profile_rules(preset, PartialSpacing(), RunOverride()) == preset


def test_run_scale_must_be_positive() -> None:
    with pytest.raises(SpacingError, match="scale"):
        RunOverride(scale=0.0)
    with pytest.raises(SpacingError, match="scale"):
        RunOverride(scale=float("nan"))
