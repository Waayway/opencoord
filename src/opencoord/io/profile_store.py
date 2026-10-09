"""User files for device profiles and spacing presets under the config directory.

Layout: ``<config>/profiles/*.toml`` and ``<config>/spacing/*.toml``. Parsing and validation are
pure and live in ``opencoord.coord``; this module only reads and writes files (atomically).
Bad files never raise on load: they are returned as ``LoadIssue`` entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

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
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return slug or "unnamed"


class ProfileStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        root = config_dir if config_dir is not None else default_config_dir()
        self.profiles_dir = root / "profiles"
        self.spacing_dir = root / "spacing"

    # -- spacing presets -------------------------------------------------------------------
    def seed_defaults(self) -> None:
        """First run only: write the built-in presets if the spacing directory does not exist.

        Later runs leave the directory alone, so deleted presets stay deleted and edits survive.
        """
        if self.spacing_dir.exists():
            return
        for preset in builtin_presets().values():
            self.save_preset(preset)

    def _preset_path(self, name: str) -> Path:
        return self.spacing_dir / f"{_slug(name)}.toml"

    def save_preset(self, preset: SpacingPreset) -> None:
        write_atomic(self._preset_path(preset.name), preset_to_toml(preset).encode("utf-8"))

    def delete_preset(self, name: str) -> None:
        self._preset_path(name).unlink(missing_ok=True)

    def reset_preset(self, name: str) -> None:
        """Restore built-in preset ``name`` (even if deleted); ``KeyError`` if not built in."""
        builtin = builtin_presets()
        if name not in builtin:
            raise KeyError(f"{name!r} is not a built-in preset (built in: {', '.join(builtin)})")
        self.save_preset(builtin[name])

    def load_presets(self) -> tuple[dict[str, SpacingPreset], list[LoadIssue]]:
        presets: dict[str, SpacingPreset] = {}
        issues: list[LoadIssue] = []
        for path in sorted(self.spacing_dir.glob("*.toml")):
            try:
                preset = parse_preset(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SpacingError) as exc:
                issues.append(LoadIssue(path, str(exc)))
                continue
            presets[preset.name] = preset
        return presets, issues

    # -- profiles --------------------------------------------------------------------------
    def _profile_path(self, name: str) -> Path:
        return self.profiles_dir / f"{_slug(name)}.toml"

    def save_profile(self, profile: DeviceProfile) -> None:
        write_atomic(self._profile_path(profile.name), profile_to_toml(profile).encode("utf-8"))

    def delete_profile(self, name: str) -> None:
        self._profile_path(name).unlink(missing_ok=True)

    def load_profiles(
        self, presets: dict[str, SpacingPreset]
    ) -> tuple[list[DeviceProfile], list[LoadIssue]]:
        profiles: list[DeviceProfile] = []
        issues: list[LoadIssue] = []
        for path in sorted(self.profiles_dir.glob("*.toml")):
            try:
                profiles.append(parse_profile(path.read_text(encoding="utf-8"), presets.keys()))
            except (OSError, UnicodeDecodeError, ProfileError) as exc:
                issues.append(LoadIssue(path, str(exc)))
        return profiles, issues
