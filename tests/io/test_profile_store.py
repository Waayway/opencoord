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


def _preset(name: str, n: int = 1) -> SpacingPreset:
    return SpacingPreset(name, "", SpacingRules(n, n, n, n, n, n))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("マイク", "ギター"),
        ("Микрофон", "Гитара"),
        ("!!!", "???"),
        ("Club / 2nd floor", "Club - 2nd floor"),
        ("Stage", "stage"),
    ],
)
def test_names_that_slug_alike_do_not_overwrite_each_other(
    store: ProfileStore, a: str, b: str
) -> None:
    store.save_preset(_preset(a, 1))
    store.save_preset(_preset(b, 2))
    presets, issues = store.load_presets()
    assert issues == []
    assert presets[a].rules.carrier == 1
    assert presets[b].rules.carrier == 2
    store.save_preset(_preset(a, 3))  # re-saving updates in place, no third file
    presets, _ = store.load_presets()
    assert len(presets) == 2
    assert presets[a].rules.carrier == 3
    store.delete_preset(b)
    assert list(store.load_presets()[0]) == [a]


def test_colliding_profile_names_are_kept_apart(store: ProfileStore) -> None:
    store.seed_defaults()
    presets, _ = store.load_presets()
    p1 = DeviceProfile("マイク", "mic", "iem", channels=(863_000_000,))
    p2 = DeviceProfile("ギター", "mic", "iem", channels=(864_000_000,))
    store.save_profile(p1)
    store.save_profile(p2)
    profiles, issues = store.load_profiles(presets)
    assert issues == []
    assert sorted(p.name for p in profiles) == sorted(["マイク", "ギター"])


def test_duplicate_internal_names_are_reported(store: ProfileStore, tmp_path: Path) -> None:
    store.save_preset(_preset("a"))
    d = tmp_path / "cfg" / "spacing"
    (d / "copy.toml").write_text((d / "a.toml").read_text(encoding="utf-8"), encoding="utf-8")
    presets, issues = store.load_presets()
    assert list(presets) == ["a"]
    assert "duplicate" in issues[0].message

    store.seed_defaults()
    presets, _ = store.load_presets()
    pdir = tmp_path / "cfg" / "profiles"
    store.save_profile(DeviceProfile("P", "mic", "iem", channels=(863_000_000,)))
    (pdir / "dup.toml").write_text((pdir / "p.toml").read_text(encoding="utf-8"), encoding="utf-8")
    profiles, issues = store.load_profiles(presets)
    assert len(profiles) == 1
    assert "duplicate" in issues[0].message


def test_rename_removes_the_old_file(store: ProfileStore, tmp_path: Path) -> None:
    store.save_preset(_preset("old", 5))
    store.rename_preset("old", _preset("new", 5))
    assert list(store.load_presets()[0]) == ["new"]
    assert len(list((tmp_path / "cfg" / "spacing").glob("*.toml"))) == 1
    store.save_preset(_preset("other"))
    with pytest.raises(FileExistsError):
        store.rename_preset("new", _preset("other"))

    presets = {"iem": builtin_presets()["iem"]}
    store.save_profile(DeviceProfile("A", "mic", "iem", channels=(863_000_000,)))
    store.rename_profile("A", DeviceProfile("B", "mic", "iem", channels=(863_000_000,)))
    assert [p.name for p in store.load_profiles(presets)[0]] == ["B"]


def test_seeding_after_a_save_keeps_the_saved_preset(store: ProfileStore) -> None:
    mine = SpacingPreset("iem", "mine", SpacingRules(1, 2, 3, 4, 5, 6))
    store.save_preset(mine)
    store.seed_defaults()
    presets, issues = store.load_presets()
    assert issues == []
    assert presets["iem"] == mine
    assert set(presets) == set(builtin_presets())
    store.delete_preset("digital")
    store.seed_defaults()
    assert "digital" not in store.load_presets()[0]
