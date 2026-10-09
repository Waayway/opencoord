"""Profile/preset editing against a real store in a temporary folder (no Dear PyGui)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencoord.coord.profiles import DeviceProfile
from opencoord.coord.spacing import builtin_presets
from opencoord.io.profile_store import ProfileStore
from opencoord.ui.profiles_actions import ProfilesActions


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "cfg")


@pytest.fixture
def pa(store: ProfileStore) -> ProfilesActions:
    actions = ProfilesActions(store)
    actions.startup()
    return actions


def new_saved(pa: ProfilesActions, name: str = "Mine") -> None:
    pa.new_from_template("generic-analog-mic")
    assert pa.draft is not None
    pa.draft.set_name(name)
    assert pa.save_profile(), pa.message


def test_startup_seeds_presets_and_lists_issues(store: ProfileStore, tmp_path: Path) -> None:
    (tmp_path / "cfg" / "profiles").mkdir(parents=True)
    (tmp_path / "cfg" / "profiles" / "broken.toml").write_text("[profile]\nname = 'x'\n")
    pa = ProfilesActions(store)
    pa.startup()
    assert pa.preset_names == sorted(builtin_presets())
    assert pa.profile_names == []
    assert [i.path.name for i in pa.issues] == ["broken.toml"]
    assert "spacing" in pa.issues[0].message


def test_new_from_template_saves_and_reloads(pa: ProfilesActions, store: ProfileStore) -> None:
    pa.new_from_template("generic-iem")
    d = pa.draft
    assert d is not None and d.is_new and d.name == "Generic IEM" and not d.dirty
    assert pa.profile_status().errors == ()
    assert pa.save_profile()
    assert pa.message == "Saved profile 'Generic IEM'"
    assert pa.profile_names == ["Generic IEM"] and not d.is_new and not d.dirty
    assert store.load_profiles(pa.presets)[0][0].name == "Generic IEM"
    # Second profile from the same template gets a free name instead of clashing.
    pa.new_from_template("generic-iem")
    assert pa.draft is not None and pa.draft.name == "Generic IEM 2"


def test_invalid_draft_does_not_save(pa: ProfilesActions) -> None:
    pa.new_blank()
    assert pa.draft is not None
    status = pa.profile_status()
    assert status.profile is None and status.errors
    assert not pa.save_profile()
    assert pa.message_is_error and pa.message.startswith("Fix the errors first")
    assert pa.profile_names == []


def test_name_clash_blocks_save_and_rename_moves_the_file(
    pa: ProfilesActions, tmp_path: Path
) -> None:
    new_saved(pa, "Alpha")
    new_saved(pa, "Beta")
    assert pa.draft is not None
    pa.draft.set_name("Alpha")
    assert pa.profile_status().errors == (
        "A profile named 'Alpha' already exists; choose another name",
    )
    pa.draft.set_name("Gamma")
    assert pa.save_profile()
    assert pa.profile_names == ["Alpha", "Gamma"]
    files = sorted(p.name for p in (tmp_path / "cfg" / "profiles").glob("*.toml"))
    assert files == ["alpha.toml", "gamma.toml"]


def test_selecting_with_unsaved_edits_asks_first(pa: ProfilesActions) -> None:
    new_saved(pa, "A")
    new_saved(pa, "B")
    assert pa.draft is not None
    pa.draft.set_name("B edited")
    pa.select_profile("A")
    assert pa.pending is not None and pa.pending.scope == "profile"
    assert pa.draft.name == "B edited"  # nothing happened yet
    pa.cancel_pending()
    assert pa.pending is None and pa.draft.name == "B edited"
    pa.select_profile("A")
    pa.confirm_pending()
    assert pa.draft.name == "A" and pa.pending is None
    pa.select_profile("B")  # clean: no question
    assert pa.pending is None and pa.draft.name == "B"


def test_clone_then_save_creates_a_second_profile(pa: ProfilesActions) -> None:
    new_saved(pa, "Orig")
    assert pa.draft is not None
    pa.draft.set_override("im3_2tx", 123)
    pa.clone_profile()
    assert pa.draft.name == "Orig copy" and pa.draft.is_new
    assert pa.save_profile()
    assert pa.profile_names == ["Orig", "Orig copy"]
    assert pa.profiles["Orig copy"].spacing_overrides.im3_2tx == 123_000
    assert pa.profiles["Orig"].spacing_overrides.im3_2tx is None


def test_delete_needs_confirmation(pa: ProfilesActions) -> None:
    new_saved(pa, "Gone")
    pa.request_delete_profile()
    assert pa.pending is not None and "Gone" in pa.pending.prompt
    assert pa.profile_names == ["Gone"]
    pa.cancel_pending()
    assert pa.profile_names == ["Gone"]
    pa.request_delete_profile()
    pa.confirm_pending()
    assert pa.profile_names == [] and pa.draft is None
    assert pa.message == "Deleted profile 'Gone'"


def test_deleting_an_unsaved_draft_just_drops_it(pa: ProfilesActions) -> None:
    pa.new_from_template("generic-iem")
    pa.request_delete_profile()
    assert pa.pending is None and pa.draft is None


def test_export_and_import_round_trip(pa: ProfilesActions, tmp_path: Path) -> None:
    pa.new_from_template("fixed-channel-set")
    assert pa.draft is not None
    out = tmp_path / "share" / "fixed"
    assert not (tmp_path / "share").exists()
    (tmp_path / "share").mkdir()
    assert pa.export_profile(out)
    assert (tmp_path / "share" / "fixed.toml").exists()
    assert pa.default_export_name() == "fixed-channel-set.toml"
    new_saved(pa, "Other")
    assert pa.import_profile(tmp_path / "share" / "fixed.toml")
    d = pa.draft
    assert d is not None and d.is_new and d.dirty and d.name == "Fixed-channel set"
    assert [g.name for g in d.groups] == ["A", "B"]
    assert "Imported fixed.toml" in pa.message
    assert pa.save_profile() and "Fixed-channel set" in pa.profile_names


def test_import_renames_on_clash_and_falls_back_for_unknown_preset(
    pa: ProfilesActions, tmp_path: Path
) -> None:
    new_saved(pa, "Dup")
    f = tmp_path / "x.toml"
    f.write_text(
        '[profile]\nname = "Dup"\nspacing = "no-such"\nchannels = [470.1, 470.2]\n',
        encoding="utf-8",
    )
    assert pa.import_profile(f)
    assert pa.draft is not None and pa.draft.name == "Dup 2"
    assert pa.draft.preset == "generic-analog"
    assert "no-such" in pa.message and "renamed" in pa.message


def test_import_errors_are_messages(pa: ProfilesActions, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[profile\n")
    assert not pa.import_profile(bad)
    assert pa.message_is_error and "not valid TOML" in pa.message
    bad.write_text('[profile]\nname = "x"\nspacing = "iem"\ntuning = [[5, 1]]\nstep_khz = 25\n')
    assert not pa.import_profile(bad)
    assert "Tuning range 1" in pa.message and "bad.toml" in pa.message
    assert not pa.import_profile(tmp_path / "missing.toml")
    assert pa.message.startswith("Cannot read missing.toml")
    assert not pa.export_profile(tmp_path / "e.toml")  # no draft


def test_export_refuses_invalid_draft(pa: ProfilesActions, tmp_path: Path) -> None:
    pa.new_blank()
    assert not pa.export_profile(tmp_path / "e.toml")
    assert not (tmp_path / "e.toml").exists()


# --- presets ---------------------------------------------------------------------------------


def test_preset_crud_clone_and_reset(pa: ProfilesActions) -> None:
    pa.select_preset("iem")
    d = pa.preset_draft
    assert d is not None and d.values["carrier"] == 600 and not d.dirty
    d.set_value("carrier", 650)
    assert pa.save_preset()
    assert pa.presets["iem"].rules.carrier == 650_000
    assert pa.preset_differs_from_builtin("iem") and pa.is_builtin_preset("iem")
    assert pa.reset_preset_to_builtin("iem")
    assert pa.presets["iem"].rules.carrier == 600_000 and not pa.preset_differs_from_builtin("iem")
    assert pa.preset_draft is not None and pa.preset_draft.values["carrier"] == 600
    pa.clone_preset()
    assert pa.preset_draft.name == "iem copy"
    assert pa.save_preset()
    assert "iem copy" in pa.presets and not pa.is_builtin_preset("iem copy")
    assert not pa.reset_preset_to_builtin("iem copy")
    assert pa.message_is_error


def test_preset_name_clash_and_validation(pa: ProfilesActions) -> None:
    pa.new_preset()
    d = pa.preset_draft
    assert d is not None and d.name == "New preset" and d.values["carrier"] == 350
    d.set_name("iem")
    assert pa.preset_errors() == ["A preset named 'iem' already exists; choose another name"]
    assert not pa.save_preset()
    d.set_name("Mine")
    d.set_value("carrier", -1)
    assert pa.preset_errors()[0].startswith("Carrier spacing")
    d.set_value("carrier", 1)
    assert pa.preset_errors() == [] and pa.save_preset()


def test_delete_preset_confirms_and_deleted_builtin_can_be_restored(pa: ProfilesActions) -> None:
    pa.select_preset("digital")
    pa.request_delete_preset()
    assert pa.pending is not None and pa.pending.scope == "preset"
    assert "restored" in pa.pending.prompt
    pa.confirm_pending()
    assert "digital" not in pa.presets and pa.preset_draft is None
    assert pa.missing_builtin_presets() == ["digital"]
    assert pa.reset_preset_to_builtin("digital")
    assert "digital" in pa.presets and pa.missing_builtin_presets() == []


def test_cannot_delete_a_preset_in_use(pa: ProfilesActions) -> None:
    new_saved(pa, "User")  # uses generic-analog
    pa.select_preset("generic-analog")
    pa.request_delete_preset()
    assert pa.pending is None and pa.message_is_error
    assert "'User'" in pa.message and "generic-analog" in pa.presets


def test_renaming_a_preset_updates_the_profiles_that_use_it(pa: ProfilesActions) -> None:
    new_saved(pa, "User")
    pa.select_preset("generic-analog")
    assert pa.preset_draft is not None
    pa.preset_draft.set_name("my analog")
    assert pa.save_preset()
    assert "generic-analog" not in pa.presets and "my analog" in pa.presets
    assert pa.profiles["User"].spacing_preset == "my analog"
    assert pa.issues == []
    assert pa.draft is not None and pa.draft.preset == "my analog" and not pa.draft.dirty
    assert "1 profile" in pa.message


def test_unsaved_preset_edits_ask_before_switching(pa: ProfilesActions) -> None:
    pa.select_preset("iem")
    assert pa.preset_draft is not None
    pa.preset_draft.set_value("carrier", 1)
    pa.select_preset("digital")
    assert pa.pending is not None and pa.pending.scope == "preset"
    pa.confirm_pending()
    assert pa.preset_draft.name == "digital"


def test_version_changes_on_every_visible_change(pa: ProfilesActions) -> None:
    v = pa.version
    pa.new_blank()
    assert pa.version > v
    v = pa.version
    pa.say("hi")
    assert pa.version > v


def test_external_say_callback_gets_messages(store: ProfileStore) -> None:
    seen: list[str] = []
    pa = ProfilesActions(store, seen.append)
    pa.startup()
    pa.select_profile("nope")
    assert seen == ["No profile named 'nope'"]


# --- failure paths ---------------------------------------------------------------------------


def test_preset_rename_with_failing_profile_update_still_finishes(
    pa: ProfilesActions, store: ProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_saved(pa, "Good")
    new_saved(pa, "Bad")
    real = store.save_profile

    def flaky(profile: DeviceProfile) -> None:
        if profile.name == "Bad":
            raise OSError("disk full")
        real(profile)

    pa.select_preset("generic-analog")
    assert pa.preset_draft is not None
    pa.preset_draft.set_name("renamed")
    monkeypatch.setattr(store, "save_profile", flaky)
    assert pa.save_preset()
    monkeypatch.undo()
    assert "renamed" in pa.presets and "generic-analog" not in pa.presets
    assert pa.preset_draft is not None and not pa.preset_draft.dirty
    assert pa.preset_draft.original_name == "renamed"
    assert pa.message_is_error
    assert pa.message.startswith("Renamed to 'renamed', but 1 profile(s) could not be updated")
    assert "'Bad' (disk full)" in pa.message
    assert "Good" in pa.profiles and pa.profiles["Good"].spacing_preset == "renamed"
    # Bad still points at the old, now missing name: it shows up as a load issue.
    assert "Bad" not in pa.profiles and [i.path.name for i in pa.issues] == ["bad.toml"]


def test_rename_mentions_unloadable_profiles_that_use_the_old_name(
    pa: ProfilesActions, tmp_path: Path
) -> None:
    (tmp_path / "cfg" / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "profiles" / "odd.toml").write_text(
        '[profile]\nname = "Odd"\nspacing = "iem"\nwhatever = 1\n'
    )
    pa.reload()
    pa.select_preset("iem")
    assert pa.preset_draft is not None
    pa.preset_draft.set_name("iem2")
    assert pa.save_preset()
    assert "odd.toml" in pa.message and "still use 'iem'" in pa.message


@pytest.mark.parametrize(
    "method", ["save_profile", "delete_profile", "save_preset", "delete_preset"]
)
def test_store_errors_become_messages(
    pa: ProfilesActions, store: ProfileStore, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    def boom(*_: object) -> None:
        raise OSError("read-only")

    new_saved(pa, "P")
    assert pa.draft is not None
    monkeypatch.setattr(store, method, boom)
    if method == "save_profile":
        pa.draft.set_name("P2")
        assert not pa.save_profile() and pa.draft.dirty
        assert pa.message == "Could not save the profile: read-only"
    elif method == "delete_profile":
        pa.request_delete_profile()
        pa.confirm_pending()
        assert pa.message == "Could not delete the profile: read-only"
        assert "P" in pa.profiles and pa.draft is not None
    elif method == "save_preset":
        pa.select_preset("digital")
        assert pa.preset_draft is not None
        pa.preset_draft.set_value("carrier", 5)
        assert not pa.save_preset() and pa.preset_draft.dirty
        assert pa.message == "Could not save the preset: read-only"
    else:
        pa.select_preset("digital")
        pa.request_delete_preset()
        pa.confirm_pending()
        assert pa.message == "Could not delete the preset: read-only"
        assert "digital" in pa.presets
    assert pa.message_is_error


def test_reload_failure_is_not_hidden_by_the_success_message(
    pa: ProfilesActions, store: ProfileStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    pa.new_from_template("generic-iem")

    def broken(*_: object) -> None:
        raise OSError("gone")

    monkeypatch.setattr(store, "load_profiles", broken)
    assert pa.save_profile()
    assert pa.message.startswith(
        "Saved profile 'Generic IEM', but the lists could not be refreshed"
    )
    assert "gone" in pa.message and pa.message_is_error


def test_reset_to_builtin_asks_first(pa: ProfilesActions) -> None:
    pa.select_preset("iem")
    assert pa.preset_draft is not None
    pa.preset_draft.set_value("carrier", 700)
    assert pa.save_preset()
    pa.request_reset_preset()
    assert pa.pending is not None and pa.pending.confirm_label == "Yes, reset"
    assert pa.presets["iem"].rules.carrier == 700_000
    pa.confirm_pending()
    assert pa.presets["iem"].rules.carrier == 600_000
    pa.clone_preset()
    assert pa.preset_draft is not None and pa.save_preset()
    pa.request_reset_preset()
    assert pa.pending is None and pa.message_is_error  # a clone is not built in


def test_preset_in_use_by_open_draft_or_broken_file_blocks_delete(
    pa: ProfilesActions, tmp_path: Path
) -> None:
    pa.new_blank()
    assert pa.draft is not None
    pa.draft.set_preset("iem")  # open, never saved
    pa.select_preset("iem")
    pa.request_delete_preset()
    assert pa.pending is None and "open, unsaved" in pa.message
    pa.draft.set_preset("digital")
    (tmp_path / "cfg" / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "profiles" / "odd.toml").write_text(
        '[profile]\nname = "Odd"\nspacing = "iem"\nwhatever = 1\n'
    )
    (tmp_path / "cfg" / "profiles" / "junk.toml").write_bytes(b"\xff\xfe")
    pa.reload()
    pa.request_delete_preset()
    assert pa.pending is None and "odd.toml (fails to load)" in pa.message
    (tmp_path / "cfg" / "profiles" / "odd.toml").unlink()
    pa.reload()
    pa.request_delete_preset()
    assert pa.pending is not None and "junk.toml" in pa.pending.prompt
