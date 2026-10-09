"""User-file storage of profiles and spacing presets."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencoord.coord.profiles import DeviceProfile, TuningRange
from opencoord.coord.spacing import SpacingPreset, SpacingRules, builtin_presets
from opencoord.io.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "cfg")


def test_first_run_seeds_the_builtin_presets(store: ProfileStore, tmp_path: Path) -> None:
    store.seed_defaults()
    files = sorted(p.name for p in (tmp_path / "cfg" / "spacing").glob("*.toml"))
    assert files == ["conservative.toml", "digital.toml", "generic-analog.toml", "iem.toml"]
    presets, issues = store.load_presets()
    assert presets == builtin_presets()
    assert issues == []


def test_seeding_does_not_resurrect_deleted_or_overwrite_edited(store: ProfileStore) -> None:
    store.seed_defaults()
    store.delete_preset("iem")
    edited = SpacingPreset("digital", "mine", SpacingRules(1, 2, 3, 4, 5, 6))
    store.save_preset(edited)
    store.seed_defaults()
    presets, _ = store.load_presets()
    assert "iem" not in presets
    assert presets["digital"] == edited


def test_reset_to_builtin_restores_shipped_values(store: ProfileStore) -> None:
    store.seed_defaults()
    store.save_preset(SpacingPreset("iem", "mine", SpacingRules(1, 1, 1, 1, 1, 1)))
    store.reset_preset("iem")
    presets, _ = store.load_presets()
    assert presets["iem"] == builtin_presets()["iem"]
    store.delete_preset("iem")
    store.reset_preset("iem")
    assert store.load_presets()[0]["iem"] == builtin_presets()["iem"]


def test_reset_unknown_preset_is_an_error(store: ProfileStore) -> None:
    with pytest.raises(KeyError, match="my-own"):
        store.reset_preset("my-own")


def test_custom_preset_round_trip_and_odd_names(store: ProfileStore) -> None:
    custom = SpacingPreset("Club / 2nd floor", "", SpacingRules(10, 20, 30, 40, 50, 60))
    store.save_preset(custom)
    assert store.load_presets()[0]["Club / 2nd floor"] == custom


def test_bad_files_are_reported_not_raised(store: ProfileStore, tmp_path: Path) -> None:
    store.seed_defaults()
    (tmp_path / "cfg" / "spacing" / "broken.toml").write_text("carrier = [", encoding="utf-8")
    presets, issues = store.load_presets()
    assert len(presets) == 4
    assert len(issues) == 1
    assert issues[0].path.name == "broken.toml"
    assert "TOML" in issues[0].message


def test_profile_save_load_delete(store: ProfileStore) -> None:
    store.seed_defaults()
    presets, _ = store.load_presets()
    p = DeviceProfile(
        "Handheld A", "mic", "generic-analog", tuning=(TuningRange(823_000_000, 832_000_000),),
        step_hz=25_000,
    )  # fmt: skip
    store.save_profile(p)
    profiles, issues = store.load_profiles(presets)
    assert profiles == [p]
    assert issues == []
    store.delete_profile("Handheld A")
    assert store.load_profiles(presets)[0] == []


def test_profile_with_missing_preset_is_an_issue_listing_presets(store: ProfileStore) -> None:
    store.seed_defaults()
    presets, _ = store.load_presets()
    p = DeviceProfile("X", "mic", "gone", channels=(863_000_000,))
    store.save_profile(p)
    profiles, issues = store.load_profiles(presets)
    assert profiles == []
    assert "gone" in issues[0].message
    assert "generic-analog" in issues[0].message


def test_loading_from_a_missing_directory_is_empty(store: ProfileStore) -> None:
    assert store.load_profiles({}) == ([], [])
    assert store.load_presets() == ({}, [])
