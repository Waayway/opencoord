"""User files for device profiles and spacing presets under the config directory.

Layout: ``<config>/profiles/*.toml`` and ``<config>/spacing/*.toml``. Parsing and validation are
pure and live in ``opencoord.coord``; this module only reads and writes files (atomically).
Bad files never raise on load: they are returned as ``LoadIssue`` entries.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir

from opencoord.coord.profiles import DeviceProfile, ProfileError, parse_profile, profile_to_toml
from opencoord.coord.spacing import (
    SpacingError,
    SpacingPreset,
    builtin_presets,
    parse_preset,
    preset_to_toml,
)
from opencoord.io.atomic import write_atomic


@dataclass(frozen=True)
class LoadIssue:
    path: Path
    message: str


def default_config_dir() -> Path:
    return Path(user_config_dir("opencoord"))


def _slug(name: str) -> str:
    """File stem for ``name``: NFC, casefolded, Unicode word characters kept.

    Case-folding makes names that differ only by case map to the same stem, which the collision
    handling in ``ProfileStore`` then resolves with a numeric suffix.
    """
    text = unicodedata.normalize("NFC", name).strip().casefold()
    slug = re.sub(r"[^\w.-]+", "-", text).strip("-.")
    return slug or "unnamed"


def file_stem(name: str) -> str:
    """The file stem the store would use for ``name`` (for suggesting export file names)."""
    return _slug(name)


def _internal_name(path: Path, kind: Literal["preset", "profile"]) -> str | None:
    """The name stored inside a file, or ``None`` if unreadable."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    head = data.get(kind)
    name = head.get("name") if isinstance(head, dict) else None
    return name.strip() if isinstance(name, str) else None


class ProfileStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        root = config_dir if config_dir is not None else default_config_dir()
        self.profiles_dir = root / "profiles"
        self.spacing_dir = root / "spacing"

    # -- shared file handling --------------------------------------------------------------
    @staticmethod
    def _find(directory: Path, name: str, kind: Literal["preset", "profile"]) -> Path | None:
        """The file in ``directory`` whose internal name is ``name``."""
        wanted = name.strip()
        for path in sorted(directory.glob("*.toml")):
            if _internal_name(path, kind) == wanted:
                return path
        return None

    @classmethod
    def _target(cls, directory: Path, name: str, kind: Literal["preset", "profile"]) -> Path:
        """Where to save ``name``: its existing file, else a free path (numeric suffix on clash)."""
        existing = cls._find(directory, name, kind)
        if existing is not None:
            return existing
        stem = _slug(name)
        path = directory / f"{stem}.toml"
        n = 2
        while path.exists():
            path = directory / f"{stem}-{n}.toml"
            n += 1
        return path

    # -- spacing presets -------------------------------------------------------------------
    def seed_defaults(self) -> None:
        """First run only: write built-in presets that are not already present.

        A ``.seeded`` marker records that this happened, so later runs leave the directory alone:
        deleted presets stay deleted and edits survive.
        """
        marker = self.spacing_dir / ".seeded"
        if marker.exists():
            return
        for preset in builtin_presets().values():
            if self._find(self.spacing_dir, preset.name, "preset") is None:
                self.save_preset(preset)
        write_atomic(marker, b"")

    def save_preset(self, preset: SpacingPreset) -> None:
        path = self._target(self.spacing_dir, preset.name, "preset")
        write_atomic(path, preset_to_toml(preset).encode("utf-8"))

    def delete_preset(self, name: str) -> None:
        path = self._find(self.spacing_dir, name, "preset")
        if path is not None:
            path.unlink()

    def rename_preset(self, old_name: str, preset: SpacingPreset) -> None:
        """Save ``preset`` (carrying the new name) and remove the file of ``old_name``.

        Profiles that reference the old name are not touched. ``FileExistsError`` if the new name
        belongs to a different existing preset.
        """
        old_path = self._rename_source(self.spacing_dir, "preset", old_name, preset.name)
        self.save_preset(preset)
        if old_path is not None:
            old_path.unlink(missing_ok=True)

    def reset_preset(self, name: str) -> None:
        """Restore built-in preset ``name`` (even if deleted); ``KeyError`` if not built in."""
        builtin = builtin_presets()
        if name not in builtin:
            raise KeyError(f"{name!r} is not a built-in preset (built in: {', '.join(builtin)})")
        self.save_preset(builtin[name])

    def load_presets(self) -> tuple[dict[str, SpacingPreset], list[LoadIssue]]:
        presets: dict[str, SpacingPreset] = {}
        paths: dict[str, Path] = {}
        issues: list[LoadIssue] = []
        for path in sorted(self.spacing_dir.glob("*.toml")):
            try:
                preset = parse_preset(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SpacingError) as exc:
                issues.append(LoadIssue(path, str(exc)))
                continue
            if preset.name in presets:
                issues.append(
                    LoadIssue(
                        path,
                        f"duplicate preset name {preset.name!r} (already defined in "
                        f"{paths[preset.name].name}); this file is ignored",
                    )
                )
                continue
            presets[preset.name] = preset
            paths[preset.name] = path
        return presets, issues

    # -- profiles --------------------------------------------------------------------------
    def save_profile(self, profile: DeviceProfile) -> None:
        path = self._target(self.profiles_dir, profile.name, "profile")
        write_atomic(path, profile_to_toml(profile).encode("utf-8"))

    def delete_profile(self, name: str) -> None:
        path = self._find(self.profiles_dir, name, "profile")
        if path is not None:
            path.unlink()

    def rename_profile(self, old_name: str, profile: DeviceProfile) -> None:
        """Save ``profile`` (carrying the new name) and remove the file of ``old_name``.

        ``FileExistsError`` if the new name belongs to a different existing profile.
        """
        old_path = self._rename_source(self.profiles_dir, "profile", old_name, profile.name)
        self.save_profile(profile)
        if old_path is not None:
            old_path.unlink(missing_ok=True)

    def load_profiles(
        self, presets: dict[str, SpacingPreset]
    ) -> tuple[list[DeviceProfile], list[LoadIssue]]:
        profiles: list[DeviceProfile] = []
        seen: dict[str, Path] = {}
        issues: list[LoadIssue] = []
        for path in sorted(self.profiles_dir.glob("*.toml")):
            try:
                profile = parse_profile(path.read_text(encoding="utf-8"), presets.keys())
            except (OSError, UnicodeDecodeError, ProfileError) as exc:
                issues.append(LoadIssue(path, str(exc)))
                continue
            if profile.name in seen:
                issues.append(
                    LoadIssue(
                        path,
                        f"duplicate profile name {profile.name!r} (already defined in "
                        f"{seen[profile.name].name}); this file is ignored",
                    )
                )
                continue
            seen[profile.name] = path
            profiles.append(profile)
        return profiles, issues

    # -- rename ----------------------------------------------------------------------------
    def _rename_source(
        self, directory: Path, kind: Literal["preset", "profile"], old: str, new: str
    ) -> Path | None:
        """The old file to remove after saving under ``new`` (``None`` if nothing to remove)."""
        if old.strip() == new.strip():
            return None
        if self._find(directory, new, kind) is not None:
            raise FileExistsError(f"a {kind} named {new!r} already exists")
        return self._find(directory, old, kind)
