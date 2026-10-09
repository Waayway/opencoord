"""Profile and spacing-preset editing session: drafts + the on-disk store (no Dear PyGui).

``ProfilesActions`` owns what the Profiles tab shows: the loaded presets and profiles, files that
failed to load, one profile draft and one preset draft, a status message and a pending confirmation
(delete, discard unsaved changes). Every file or validation failure becomes a message (never an
exception). The panel only renders this object and calls its methods.

``version`` changes whenever lists, drafts being swapped, the message or the pending confirmation
change; edits inside a draft are tracked by the draft's own ``revision``.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from opencoord.coord.profiles import (
    DeviceProfile,
    ProfileError,
    builtin_templates,
    profile_from_dict,
    profile_to_toml,
)
from opencoord.coord.spacing import SpacingPreset, builtin_presets
from opencoord.io.atomic import write_atomic
from opencoord.io.profile_store import LoadIssue, ProfileStore, file_stem
from opencoord.ui.profile_editor import (
    PresetDraft,
    Preview,
    ProfileDraft,
    friendly,
    unique_name,
)

log = logging.getLogger(__name__)

PROFILE_SUFFIX = ".toml"


@dataclass
class Pending:
    """A confirmation the user has to click (shown inline in the panel's sub-tab ``scope``)."""

    scope: str  # "profile" | "preset"
    prompt: str
    confirm_label: str
    action: Callable[[], None]


@dataclass(frozen=True)
class ProfileStatus:
    profile: DeviceProfile | None
    errors: tuple[str, ...]
    preview: Preview | None


class ProfilesActions:
    def __init__(self, store: ProfileStore, say: Callable[[str], None] | None = None) -> None:
        self.store = store
        self._say = say
        self.presets: dict[str, SpacingPreset] = {}
        self.profiles: dict[str, DeviceProfile] = {}
        self.issues: list[LoadIssue] = []
        self.draft: ProfileDraft | None = None
        self.preset_draft: PresetDraft | None = None
        self.pending: Pending | None = None
        self.message = ""
        self.message_is_error = False
        self.version = 0
        self._status_key: tuple[int, int, int] | None = None
        self._status: ProfileStatus | None = None

    # --- plumbing -------------------------------------------------------------------------

    def _bump(self) -> None:
        self.version += 1

    def say(self, message: str, *, error: bool = False) -> None:
        self.message = message
        self.message_is_error = error
        self._bump()
        if self._say is not None:
            self._say(message)

    def startup(self) -> None:
        """Seed the built-in presets on the first run, then load everything."""
        try:
            self.store.seed_defaults()
        except OSError as exc:
            log.warning("could not seed the spacing presets", exc_info=True)
            self.say(f"Could not write the default spacing presets: {exc}", error=True)
        self.reload()

    def reload(self, *, report: bool = True) -> str | None:
        """Re-read presets and profiles from disk (bad files end up in ``issues``).

        Returns ``None`` on success, else the error text (also shown unless ``report`` is false,
        which lets a caller fold it into its own message).
        """
        try:
            self.presets, preset_issues = self.store.load_presets()
            profiles, profile_issues = self.store.load_profiles(self.presets)
        except OSError as exc:
            log.warning("could not read the profile folders", exc_info=True)
            error = f"Could not read the profile folders: {exc}"
            if report:
                self.say(error, error=True)
            return error
        self.profiles = {p.name: p for p in sorted(profiles, key=lambda p: p.name.casefold())}
        self.issues = [*preset_issues, *profile_issues]
        self._bump()
        return None

    def _done(self, message: str, reload_error: str | None) -> None:
        """Say ``message``; a failed reload is added instead of being overwritten by it."""
        if reload_error:
            self.say(
                f"{message}, but the lists could not be refreshed ({reload_error})", error=True
            )
        else:
            self.say(message)

    @property
    def preset_names(self) -> list[str]:
        return sorted(self.presets)

    @property
    def profile_names(self) -> list[str]:
        return list(self.profiles)

    @staticmethod
    def template_choices() -> list[tuple[str, str]]:
        """``(key, display name)`` of the built-in templates."""
        return [(key, p.name) for key, p in sorted(builtin_templates().items())]

    def _default_preset(self) -> str:
        names = self.preset_names
        return "generic-analog" if "generic-analog" in names else (names[0] if names else "")

    # --- pending confirmations -------------------------------------------------------------

    def confirm_pending(self) -> None:
        pending, self.pending = self.pending, None
        self._bump()
        if pending is not None:
            pending.action()

    def cancel_pending(self) -> None:
        if self.pending is not None:
            self.pending = None
            self._bump()

    def _ask(self, scope: str, prompt: str, label: str, action: Callable[[], None]) -> None:
        self.pending = Pending(scope, prompt, label, action)
        self._bump()

    def _guard_profile(self, action: Callable[[], None]) -> None:
        """Run ``action`` now, or after a confirmation when the profile draft has unsaved edits."""
        d = self.draft
        if d is not None and d.dirty:
            self._ask(
                "profile",
                f"'{d.name.strip() or 'This profile'}' has unsaved changes. Discard them?",
                "Discard changes",
                action,
            )
        else:
            self.pending = None
            action()

    def _guard_preset(self, action: Callable[[], None]) -> None:
        d = self.preset_draft
        if d is not None and d.dirty:
            self._ask(
                "preset",
                f"'{d.name.strip() or 'This preset'}' has unsaved changes. Discard them?",
                "Discard changes",
                action,
            )
        else:
            self.pending = None
            action()

    # --- profiles: open / create -------------------------------------------------------------

    def _set_draft(self, draft: ProfileDraft | None) -> None:
        self.draft = draft
        self._bump()

    def new_blank(self) -> None:
        def go() -> None:
            d = ProfileDraft.blank(self._default_preset())
            d.name = unique_name("New profile", self.profiles)
            d.mark_saved(None)
            self._set_draft(d)

        self._guard_profile(go)

    def new_from_template(self, key: str) -> None:
        templates = builtin_templates()
        if key not in templates:
            self.say(f"Unknown template {key!r}", error=True)
            return
        template = templates[key]

        def go() -> None:
            preset = template.spacing_preset
            if preset not in self.presets:
                preset = self._default_preset()
            d = ProfileDraft.from_profile(replace(template, spacing_preset=preset), None)
            d.name = unique_name(template.name, self.profiles)
            d.mark_saved(None)
            self._set_draft(d)
            if preset != template.spacing_preset:
                self.say(
                    f"Spacing preset '{template.spacing_preset}' does not exist (anymore); "
                    f"using '{preset}'"
                )

        self._guard_profile(go)

    def select_profile(self, name: str) -> None:
        if name not in self.profiles:
            self.say(f"No profile named '{name}'", error=True)
            return
        if self.draft is not None and self.draft.original_name == name and not self.draft.dirty:
            return
        self._guard_profile(
            lambda: self._set_draft(ProfileDraft.from_profile(self.profiles[name], name))
        )

    def clone_profile(self) -> None:
        if self.draft is None:
            return
        self._set_draft(self.draft.clone(self.profiles))
        self.pending = None
        self.say("Cloned; give it a name and save")

    def close_profile(self) -> None:
        self._guard_profile(lambda: self._set_draft(None))

    # --- profiles: validate / save / delete ---------------------------------------------------

    def _taken_profile_names(self) -> set[str]:
        d = self.draft
        return {n for n in self.profiles if d is None or n != d.original_name}

    def profile_status(self) -> ProfileStatus:
        """Built profile, readable errors and preview of the current draft (cached per edit)."""
        d = self.draft
        if d is None:
            return ProfileStatus(None, (), None)
        key = (id(d), d.revision, self.version)
        if self._status_key == key and self._status is not None:
            return self._status
        names = self.presets.keys()
        profile, errors = d.build(names, self._taken_profile_names())
        status = ProfileStatus(profile, tuple(errors), d.preview(names))
        self._status_key, self._status = key, status
        return status

    def save_profile(self) -> bool:
        d = self.draft
        if d is None:
            return False
        status = self.profile_status()
        if status.profile is None:
            self.say("Fix the errors first: " + status.errors[0], error=True)
            return False
        profile = status.profile
        try:
            if d.original_name is not None and d.original_name != profile.name:
                self.store.rename_profile(d.original_name, profile)
            else:
                self.store.save_profile(profile)
        except FileExistsError as exc:
            self.say(str(exc), error=True)
            return False
        except OSError as exc:
            log.warning("could not save profile %s", profile.name, exc_info=True)
            self.say(f"Could not save the profile: {exc}", error=True)
            return False
        d.mark_saved(profile.name)
        self._done(f"Saved profile '{profile.name}'", self.reload(report=False))
        return True

    def request_delete_profile(self) -> None:
        d = self.draft
        if d is None:
            return
        if d.is_new:  # nothing stored: just drop the draft
            self._guard_profile(lambda: self._set_draft(None))
            return
        name = d.original_name or d.name
        self._ask(
            "profile",
            f"Delete profile '{name}' permanently?",
            "Yes, delete",
            lambda: self._delete_profile(name),
        )

    def _delete_profile(self, name: str) -> None:
        try:
            self.store.delete_profile(name)
        except OSError as exc:
            self.say(f"Could not delete the profile: {exc}", error=True)
            return
        error = self.reload(report=False)
        if self.draft is not None and self.draft.original_name == name:
            self._set_draft(None)
        self._done(f"Deleted profile '{name}'", error)

    # --- profiles: import / export -------------------------------------------------------------

    def export_profile(self, path: Path) -> bool:
        status = self.profile_status()
        if status.profile is None:
            self.say("Fix the errors before exporting the profile", error=True)
            return False
        if not path.suffix:
            path = path.with_suffix(PROFILE_SUFFIX)
        try:
            write_atomic(path, profile_to_toml(status.profile).encode("utf-8"))
        except OSError as exc:
            self.say(f"Could not export the profile: {exc}", error=True)
            return False
        self.say(f"Exported '{status.profile.name}' to {path}")
        return True

    def default_export_name(self) -> str:
        d = self.draft
        return (
            f"{file_stem(d.name)}{PROFILE_SUFFIX}" if d is not None else f"profile{PROFILE_SUFFIX}"
        )

    def import_profile(self, path: Path) -> bool:
        """Open a profile TOML file as a new unsaved draft (review it, then save)."""
        try:
            text = path.read_text(encoding="utf-8")
            data = tomllib.loads(text)
        except (OSError, UnicodeDecodeError) as exc:
            self.say(f"Cannot read {path.name}: {exc}", error=True)
            return False
        except tomllib.TOMLDecodeError as exc:
            self.say(f"{path.name} is not valid TOML: {exc}", error=True)
            return False
        notes: list[str] = []
        head = data.get("profile")
        if isinstance(head, dict) and self.presets and head.get("spacing") not in self.presets:
            fallback = self._default_preset()
            notes.append(f"preset '{head.get('spacing')}' not found, using '{fallback}'")
            head["spacing"] = fallback
        try:
            profile = profile_from_dict(data, self.presets.keys())
        except ProfileError as exc:
            self.say(f"{path.name}: {friendly(str(exc))}", error=True)
            return False

        def go() -> None:
            d = ProfileDraft.from_profile(profile, None)
            fresh = unique_name(profile.name, self.profiles)
            if fresh != profile.name:
                d.name = fresh
                notes.append(f"renamed to '{fresh}' (the name was taken)")
            d.mark_saved(None)
            d.treat_as_edited()  # imported content is unsaved work
            self._set_draft(d)
            extra = f" ({'; '.join(notes)})" if notes else ""
            self.say(f"Imported {path.name}{extra}. Review it and save.")

        self._guard_profile(go)
        return True

    # --- spacing presets ---------------------------------------------------------------------

    def _set_preset_draft(self, draft: PresetDraft | None) -> None:
        self.preset_draft = draft
        self._bump()

    def new_preset(self) -> None:
        base = self.presets.get(self._default_preset())
        self._guard_preset(lambda: self._set_preset_draft(PresetDraft.blank(base, self.presets)))

    def select_preset(self, name: str) -> None:
        if name not in self.presets:
            self.say(f"No spacing preset named '{name}'", error=True)
            return
        d = self.preset_draft
        if d is not None and d.original_name == name and not d.dirty:
            return
        self._guard_preset(
            lambda: self._set_preset_draft(PresetDraft.from_preset(self.presets[name], name))
        )

    def clone_preset(self) -> None:
        if self.preset_draft is None:
            return
        self._set_preset_draft(self.preset_draft.clone(self.presets))
        self.pending = None
        self.say("Cloned; give it a name and save")

    def _taken_preset_names(self) -> set[str]:
        d = self.preset_draft
        return {n for n in self.presets if d is None or n != d.original_name}

    def preset_errors(self) -> list[str]:
        d = self.preset_draft
        return [] if d is None else d.validate(self._taken_preset_names())

    def profiles_using(self, preset_name: str) -> list[str]:
        return [n for n, p in self.profiles.items() if p.spacing_preset == preset_name]

    def _issue_users(self, preset_name: str) -> tuple[list[str], list[str]]:
        """Profile files that failed to load: ``(names using the preset, unreadable ones)``."""
        using: list[str] = []
        unknown: list[str] = []
        for issue in self.issues:
            if issue.path.parent != self.store.profiles_dir:
                continue
            try:
                head = tomllib.loads(issue.path.read_text(encoding="utf-8")).get("profile")
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                unknown.append(issue.path.name)
                continue
            if isinstance(head, dict) and head.get("spacing") == preset_name:
                using.append(issue.path.name)
        return using, unknown

    def save_preset(self) -> bool:
        d = self.preset_draft
        if d is None:
            return False
        preset, errors = d.build(self._taken_preset_names())
        if preset is None:
            self.say("Fix the errors first: " + errors[0], error=True)
            return False
        old = d.original_name
        renamed = old is not None and old != preset.name
        try:
            if renamed and old is not None:
                self.store.rename_preset(old, preset)
            else:
                self.store.save_preset(preset)
        except FileExistsError as exc:
            self.say(str(exc), error=True)
            return False
        except OSError as exc:
            log.warning("could not save preset %s", preset.name, exc_info=True)
            self.say(f"Could not save the preset: {exc}", error=True)
            return False
        d.mark_saved(preset.name)
        # The rename is done. Profiles point at presets by name, so follow it; one failing profile
        # must not stop the others, and the lists are refreshed whatever happens.
        updated: list[str] = []
        failed: list[str] = []
        if renamed and old is not None:
            for pname in self.profiles_using(old):
                try:
                    self.store.save_profile(
                        replace(self.profiles[pname], spacing_preset=preset.name)
                    )
                    updated.append(pname)
                except OSError as exc:
                    log.warning("could not update profile %s", pname, exc_info=True)
                    failed.append(f"'{pname}' ({exc})")
            stale, _unknown = self._issue_users(old)
            if self.draft is not None and self.draft.preset == old:
                was_dirty = self.draft.dirty
                self.draft.set_preset(preset.name)
                if not was_dirty:
                    self.draft.mark_saved(self.draft.original_name)
        else:
            stale = []
        error = self.reload(report=False)
        if renamed and (failed or stale):
            problems = []
            if failed:
                problems.append(
                    f"{len(failed)} profile(s) could not be updated: {', '.join(failed)}"
                )
            if stale:
                problems.append(
                    f"{len(stale)} profile file(s) that fail to load still use '{old}': "
                    + ", ".join(stale)
                )
            self._done(f"Renamed to '{preset.name}', but " + "; ".join(problems), error)
            self.message_is_error = True
            return True
        suffix = f"; {len(updated)} profile(s) now use the new name" if updated else ""
        self._done(f"Saved spacing preset '{preset.name}'{suffix}", error)
        return True

    def request_delete_preset(self) -> None:
        d = self.preset_draft
        if d is None:
            return
        if d.is_new:
            self._guard_preset(lambda: self._set_preset_draft(None))
            return
        name = d.original_name or d.name
        users = self.profiles_using(name)
        files, unknown = self._issue_users(name)
        users += [f"{f} (fails to load)" for f in files]
        if (
            self.draft is not None
            and self.draft.preset == name
            and not any(self.draft.original_name == u for u in users)
        ):
            users.append(f"{self.draft.name.strip() or 'the open profile'} (open, unsaved)")
        if users:
            shown = ", ".join(f"'{u}'" for u in users[:5]) + (", ..." if len(users) > 5 else "")
            self.say(
                f"Cannot delete '{name}': used by {shown}. Pick another preset there first.",
                error=True,
            )
            return
        hint = " It can be restored later." if name in builtin_presets() else ""
        if unknown:
            hint += f" Unreadable profile files might use it: {', '.join(unknown[:5])}."
        self._ask(
            "preset",
            f"Delete spacing preset '{name}' permanently?{hint}",
            "Yes, delete",
            lambda: self._delete_preset(name),
        )

    def _delete_preset(self, name: str) -> None:
        try:
            self.store.delete_preset(name)
        except OSError as exc:
            self.say(f"Could not delete the preset: {exc}", error=True)
            return
        error = self.reload(report=False)
        if self.preset_draft is not None and self.preset_draft.original_name == name:
            self._set_preset_draft(None)
        self._done(f"Deleted spacing preset '{name}'", error)

    # built-in presets

    @staticmethod
    def is_builtin_preset(name: str) -> bool:
        return name in builtin_presets()

    def preset_differs_from_builtin(self, name: str) -> bool:
        builtin = builtin_presets().get(name)
        return builtin is not None and self.presets.get(name) != builtin

    def missing_builtin_presets(self) -> list[str]:
        return [n for n in builtin_presets() if n not in self.presets]

    def request_reset_preset(self) -> None:
        """Ask before overwriting the open built-in preset with its shipped values."""
        d = self.preset_draft
        if d is None or d.original_name is None:
            return
        name = d.original_name
        if not self.is_builtin_preset(name):
            self.say(f"'{name}' is not a built-in preset", error=True)
            return
        self._ask(
            "preset",
            f"Reset '{name}' to its built-in values? Your values (and unsaved edits) are lost.",
            "Yes, reset",
            lambda: self._reset_confirmed(name),
        )

    def _reset_confirmed(self, name: str) -> None:
        self.reset_preset_to_builtin(name)

    def reset_preset_to_builtin(self, name: str) -> bool:
        """Write the shipped values of built-in preset ``name`` (also restores a deleted one)."""
        try:
            self.store.reset_preset(name)
        except KeyError as exc:
            self.say(str(exc.args[0]), error=True)
            return False
        except OSError as exc:
            self.say(f"Could not reset the preset: {exc}", error=True)
            return False
        error = self.reload(report=False)
        d = self.preset_draft
        if d is not None and d.original_name == name and name in self.presets:
            self._set_preset_draft(PresetDraft.from_preset(self.presets[name], name))
        self._done(f"Spacing preset '{name}' is back to its built-in values", error)
        return True


__all__ = ["Pending", "ProfileStatus", "ProfilesActions"]
