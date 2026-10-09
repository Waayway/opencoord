"""Pure editor logic for profiles and spacing presets (no Dear PyGui)."""

from __future__ import annotations

import pytest

from opencoord.coord.profiles import builtin_templates, candidates, profile_to_toml
from opencoord.coord.spacing import SpacingRules, builtin_presets
from opencoord.ui.profile_editor import (
    PresetDraft,
    ProfileDraft,
    format_mhz,
    parse_channel_text,
    unique_name,
)

PRESETS = set(builtin_presets())
MHZ = 1_000_000


def tuning_draft() -> ProfileDraft:
    d = ProfileDraft.blank("generic-analog")
    d.set_name("Mine")
    d.set_range(0, 823.0, 832.0)
    d.set_step_khz(25)
    return d


# --- paste parsing ---------------------------------------------------------------------------


def test_paste_mixed_separators_sorted_and_deduped() -> None:
    r = parse_channel_text("470.125, 470.250\n471")
    assert r.values_hz == (470_125_000, 470_250_000, 471_000_000)
    assert r.ok and r.duplicates == 0


@pytest.mark.parametrize("sep", ["\n", ", ", ";", " ", "\t", " , ", ";\n", "\r\n", ",\n"])
def test_paste_accepts_every_separator(sep: str) -> None:
    assert parse_channel_text(sep.join(["606.5", "606.1", "606.3"])).values_hz == (
        606_100_000,
        606_300_000,
        606_500_000,
    )


def test_paste_decimal_comma_and_ambiguity() -> None:
    assert parse_channel_text("470,125").values_hz == (470_125_000,)
    assert parse_channel_text("470, 125").values_hz == (125_000_000, 470_000_000)
    assert parse_channel_text("470;125").values_hz == (125_000_000, 470_000_000)
    assert parse_channel_text("470\t125").values_hz == (125_000_000, 470_000_000)
    assert parse_channel_text("470\n125").values_hz == (125_000_000, 470_000_000)
    assert parse_channel_text("470,125 MHz").values_hz == (470_125_000,)
    mixed = parse_channel_text("470,125; 471,5")
    assert mixed.ok and mixed.values_hz == (470_125_000, 471_500_000)
    bad = parse_channel_text("470,125,470,250")
    assert bad.values_hz == () and len(bad.errors) == 1
    assert bad.errors[0].startswith("'470,125,470,250' is ambiguous: use '.' or ','")
    assert parse_channel_text("470.125,470.250").errors[0].endswith("between values")
    assert parse_channel_text("470.125, 470.250\n471").values_hz == (
        470_125_000,
        470_250_000,
        471_000_000,
    )


def test_paste_dedupes_and_counts_duplicates() -> None:
    r = parse_channel_text("470.1 470.100 470.1000 471")
    assert r.values_hz == (470_100_000, 471_000_000) and r.duplicates == 2


def test_paste_junk_tokens_give_one_error_each_and_keep_the_good_ones() -> None:
    r = parse_channel_text("470.1 abc -5 0 1e999999 nan inf 12,x 471")
    assert r.values_hz == (470_100_000, 471_000_000)
    assert len(r.errors) == 7
    assert "'abc' is not a number" in r.errors[0]
    assert any("'-5' must be above 0" in e for e in r.errors)
    assert any("'1e999999' is above the limit" in e for e in r.errors)
    assert any("'nan'" in e for e in r.errors) and any(
        "'12,x' is not a number" in e for e in r.errors
    )


def test_paste_accepts_mhz_suffix_and_blank_text() -> None:
    assert parse_channel_text("470.125MHz, 471 MHz").values_hz == (470_125_000, 471_000_000)
    r = parse_channel_text("  \n ")
    assert r.values_hz == () and r.ok


def test_paste_tiny_value_is_rejected() -> None:
    assert parse_channel_text("0.0000001").errors


def test_format_mhz_is_exact() -> None:
    assert format_mhz(470_125_000) == "470.125"
    assert format_mhz(471_000_000) == "471"
    assert format_mhz(470_000_001) == "470.000001"


# --- drafts: building through the validating parse path ---------------------------------------


def test_tuning_draft_builds_and_previews() -> None:
    d = tuning_draft()
    profile, errors = d.build(PRESETS)
    assert errors == [] and profile is not None
    assert profile.tuning[0].start_hz == 823 * MHZ and profile.step_hz == 25_000
    preview = d.preview(PRESETS)
    assert preview is not None and preview.count == 361
    assert (preview.low_hz, preview.high_hz) == (823 * MHZ, 832 * MHZ)
    assert preview.text == "361 candidate frequencies, 823 - 832 MHz"


def test_validation_messages_are_readable() -> None:
    d = ProfileDraft.blank("generic-analog")  # one empty range 0 - 0
    assert d.validate(PRESETS) == [
        "Tuning range 1: frequency must be above 0 and at most 1e+06 MHz, got 0.0"
    ]
    d.set_range(0, 830, 820)
    assert "Tuning range 1: start must be below stop" in d.validate(PRESETS)[0]
    d.set_range(0, 820, 830)
    d.set_step_khz(0)
    assert d.validate(PRESETS)[0].startswith("Step: must be above 0 kHz")
    d.set_step_khz(25)
    d.set_name("  ")
    assert d.validate(PRESETS) == ["Name: a non-empty text is required"]
    d.set_name("x")
    d.set_preset("nope")
    msg = d.validate(PRESETS)[0]
    assert msg.startswith("Spacing preset: unknown preset 'nope'") and "generic-analog" in msg
    d.set_preset("generic-analog")
    d.remove_range(0)
    assert d.validate(PRESETS) == ["Add at least one tuning range"]
    assert d.preview(PRESETS) is None


def test_too_many_candidates_is_reported() -> None:
    d = tuning_draft()
    d.set_range(0, 100, 900)
    d.set_step_khz(1)
    assert "too many" in d.validate(PRESETS)[0]


def test_name_clash_is_an_error() -> None:
    d = tuning_draft()
    assert d.validate(PRESETS, taken_names={"Mine"}) == [
        "A profile named 'Mine' already exists; choose another name"
    ]


def test_channels_mode_reports_paste_errors_and_empty() -> None:
    d = tuning_draft()
    d.set_mode("channels")
    assert d.validate(PRESETS) == ["Paste at least one channel (MHz)"]
    d.paste_channels("470.125, oops\n471")
    errors = d.validate(PRESETS)
    assert errors == ["Channels: 'oops' is not a number (use MHz, e.g. 470.125 or 470,125)"]
    d.paste_channels("470.125, 470.250\n471")
    profile, errors = d.build(PRESETS)
    assert errors == [] and profile is not None
    assert profile.channels == (470_125_000, 470_250_000, 471_000_000)
    assert profile.tuning == () and profile.step_hz is None
    p = d.preview(PRESETS)
    assert p is not None and p.count == 3 and p.per_group == ()


def test_paste_append_merges_and_tidy_normalises() -> None:
    d = ProfileDraft.blank("generic-analog")
    d.set_mode("channels")
    d.paste_channels("471, 470.5")
    d.paste_channels("470.5 469.9", append=True)
    assert d.channels.values == (469_900_000, 470_500_000, 471_000_000)
    assert d.tidy_channels()
    assert d.channels.text == "469.9\n470.5\n471"
    d.paste_channels("bad")
    assert not d.tidy_channels() and d.channels.text == "bad"


def test_groups_mode_counts_per_group() -> None:
    d = ProfileDraft.blank("generic-analog")
    d.set_mode("groups")
    assert d.validate(PRESETS) == ["Add at least one group with channels"]
    a = d.add_group("A")
    b = d.add_group()
    assert d.groups[b].name == "Group"
    d.rename_group(b, "B")
    d.paste_channels("470.1 470.2 470.3", group=a)
    d.paste_channels("470.3, 471", group=b)
    profile, errors = d.build(PRESETS)
    assert errors == [] and profile is not None
    assert [(g.name, len(g.channels)) for g in profile.groups] == [("A", 3), ("B", 2)]
    p = d.preview(PRESETS)
    assert p is not None and p.count == 4 and p.per_group == (("A", 3), ("B", 2))
    assert [c.groups for c in candidates(profile)][2] == ("A", "B")
    d.rename_group(b, "A")
    assert d.validate(PRESETS) == ["Group 'A': name is used twice"]
    d.rename_group(b, "")
    assert d.validate(PRESETS) == ["Group 2 name: a non-empty text is required"]
    d.rename_group(b, "B")
    d.paste_channels("", group=b)
    assert "needs at least one channel" in d.validate(PRESETS)[0]
    d.paste_channels("x", group=b)
    assert d.validate(PRESETS) == [
        "Group B: 'x' is not a number (use MHz, e.g. 470.125 or 470,125)"
    ]
    d.remove_group(b)
    assert d.validate(PRESETS) == []


def test_mode_switch_keeps_other_data_and_warns() -> None:
    d = tuning_draft()
    d.paste_channels("470.1 470.2")
    d.add_group("A")
    d.set_mode("channels")
    warning = d.dropped_warning()
    assert warning is not None and "1 tuning range" in warning and "1 group" in warning
    assert "Saving drops" in warning
    profile, _ = d.build(PRESETS)
    assert profile is not None and profile.tuning == () and profile.groups == ()
    d.set_mode("tuning")
    warning = d.dropped_warning()
    assert warning is not None and "2 channels" in warning and "tuning range" not in warning
    assert d.ranges[0].start_mhz == 823.0  # kept
    d.channels.set_text("")
    d.groups.clear()
    assert d.dropped_warning() is None


def test_invalid_data_in_unselected_mode_does_not_block_saving() -> None:
    d = tuning_draft()
    d.paste_channels("garbage")
    assert d.validate(PRESETS) == []


# --- overrides -------------------------------------------------------------------------------


def test_override_set_clear_and_effective_spacing() -> None:
    rules = builtin_presets()["generic-analog"].rules
    d = tuning_draft()
    rows = {r.name: r for r in d.effective_spacing(rules)}
    assert rows["im3_2tx"].effective_khz == 100 and not rows["im3_2tx"].overridden
    d.set_override("im3_2tx", 150)
    d.set_override("im7_2tx", 0)
    rows = {r.name: r for r in d.effective_spacing(rules)}
    assert (rows["im3_2tx"].preset_khz, rows["im3_2tx"].effective_khz) == (100, 150)
    assert rows["im3_2tx"].overridden and rows["im7_2tx"].effective_khz == 0
    profile, errors = d.build(PRESETS)
    assert errors == [] and profile is not None
    assert profile.spacing_overrides.im3_2tx == 150_000
    assert profile.spacing_overrides.im7_2tx == 0 and profile.spacing_overrides.carrier is None
    d.clear_override("im3_2tx")
    d.clear_override("im7_2tx")
    profile, _ = d.build(PRESETS)
    assert profile is not None and profile.spacing_overrides.is_empty()
    assert d.effective_spacing(None)[0].effective_khz is None
    with pytest.raises(ValueError):
        d.set_override("bogus", 1)


def test_negative_override_is_a_readable_error() -> None:
    d = tuning_draft()
    d.set_override("carrier", -5)
    msg = d.validate(PRESETS)[0]
    assert msg.startswith("Carrier spacing") and "[spacing]" not in msg
    rows = {r.name: r for r in d.effective_spacing(SpacingRules(carrier=350_000))}
    assert rows["carrier"].effective_khz == 350 and not rows["carrier"].overridden


# --- load / templates / dirty / clone --------------------------------------------------------


@pytest.mark.parametrize("key", sorted(builtin_templates()))
def test_every_template_round_trips_through_the_draft(key: str) -> None:
    template = builtin_templates()[key]
    d = ProfileDraft.from_profile(template, None)
    profile, errors = d.build(PRESETS)
    assert errors == [] and profile == template
    assert profile_to_toml(profile) == profile_to_toml(template)


def test_dirty_tracking_and_mark_saved() -> None:
    template = builtin_templates()["generic-analog-mic"]
    d = ProfileDraft.from_profile(template, "Generic analog mic")
    assert not d.dirty and not d.is_new
    d.set_name("Other")
    assert d.dirty
    d.set_name("Generic analog mic")
    assert not d.dirty
    d.add_range(500, 510)
    assert d.dirty
    d.mark_saved("Generic analog mic")
    assert not d.dirty
    fresh = ProfileDraft.from_profile(template, None)
    assert fresh.unsaved and not fresh.dirty  # never stored, but nothing to lose


def test_structure_version_only_changes_for_row_changes() -> None:
    d = tuning_draft()
    s, r = d.structure_version, d.revision
    d.set_name("x")
    d.set_range(0, 1, 2)
    assert d.structure_version == s and d.revision > r
    d.add_range()
    d.set_mode("channels")
    assert d.structure_version == s + 2


def test_clone_copies_edits_with_unique_name() -> None:
    d = tuning_draft()
    d.set_override("carrier", 400)
    d.set_mode("groups")
    d.add_group("A")
    d.paste_channels("470.1 470.2", group=0)
    c = d.clone({"Mine", "Mine copy"})
    assert c.name == "Mine copy 2" and c.is_new and c.dirty and c.unsaved
    assert c.overrides["carrier"] == 400 and c.mode == "groups"
    assert c.groups[0].channels.values == (470_100_000, 470_200_000)
    c.paste_channels("471", group=0)
    assert d.groups[0].channels.values == (470_100_000, 470_200_000)  # independent


def test_unique_name() -> None:
    assert unique_name("A", {"B"}) == "A"
    assert unique_name("A", {"A", "A 2"}) == "A 3"
    assert unique_name("  ", set()) == "Unnamed"


def test_set_kind_and_mode_validate_input() -> None:
    d = tuning_draft()
    d.set_kind("iem")
    assert d.kind == "iem"
    with pytest.raises(ValueError):
        d.set_kind("tv")
    with pytest.raises(ValueError):
        d.set_mode("other")


# --- spacing preset draft --------------------------------------------------------------------


def test_preset_draft_build_and_errors() -> None:
    base = builtin_presets()["iem"]
    d = PresetDraft.blank(base, {"New preset"})
    assert d.name == "New preset 2" and d.is_new and d.dirty
    assert d.values["carrier"] == 600
    d.set_name("Mine")
    d.set_description("d")
    d.set_value("im3_2tx", 210.5)
    preset, errors = d.build(set())
    assert errors == [] and preset is not None
    assert preset.rules.im3_2tx == 210_500 and preset.description == "d"
    d.set_value("im3_2tx", -1)
    assert d.validate(set())[0].startswith("3rd order, 2 transmitters")
    d.set_value("im3_2tx", 1)
    assert d.validate({"Mine"}) == ["A preset named 'Mine' already exists; choose another name"]
    d.set_name(" ")
    assert d.validate(set()) == ["Name: a non-empty text is required"]


def test_preset_draft_dirty_and_clone() -> None:
    p = builtin_presets()["digital"]
    d = PresetDraft.from_preset(p, "digital")
    assert not d.dirty
    d.set_value("carrier", 1)
    assert d.dirty
    c = d.clone({"digital copy"})
    assert c.name == "digital copy 2" and c.values["carrier"] == 1 and c.is_new
