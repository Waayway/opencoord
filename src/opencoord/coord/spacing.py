"""Spacing rules: pure data, parsing, serialising and resolution (no file I/O).

Spacing is data, never constants. All values are ``int`` Hz internally and kHz in TOML
(``0`` = rule disabled). Resolution order for one profile:

1. the profile's preset (``SpacingRules``),
2. the profile's overrides (``PartialSpacing``; set fields replace the preset value),
3. the run override (``RunOverride``): its ``values`` replace fields, then its ``scale``
   multiplies every field of the result (rounded to whole Hz; 0 stays 0).

For two different profiles ``SpacingRules.resolve(a, b)`` takes the stricter (larger) value
per field.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

import tomli_w

from opencoord.coord import spacing_presets

_FIELDS = ("carrier", "im3_2tx", "im3_3tx", "im5_2tx", "im7_2tx", "im5_3tx")


class SpacingError(ValueError):
    """Invalid spacing data; the message names the field and value."""


@dataclass(frozen=True)
class SpacingRules:
    """Minimum spacings in Hz per rule; 0 disables the rule."""

    carrier: int = 0
    im3_2tx: int = 0
    im3_3tx: int = 0
    im5_2tx: int = 0
    im7_2tx: int = 0
    im5_3tx: int = 0  # advanced; off in every seeded preset

    FIELDS: ClassVar[tuple[str, ...]] = _FIELDS

    def resolve(self, other: SpacingRules) -> SpacingRules:
        """Per-field maximum (the stricter rule) for a pair of devices."""
        return SpacingRules(**{f: max(getattr(self, f), getattr(other, f)) for f in _FIELDS})

    def scaled(self, factor: float) -> SpacingRules:
        """Every spacing multiplied by ``factor`` and rounded to whole Hz."""
        return SpacingRules(**{f: round(getattr(self, f) * factor) for f in _FIELDS})

    def with_overrides(self, partial: PartialSpacing) -> SpacingRules:
        """Fields set in ``partial`` replace the ones here."""
        changes = {f: v for f in _FIELDS if (v := getattr(partial, f)) is not None}
        return replace(self, **changes)


@dataclass(frozen=True)
class PartialSpacing:
    """Optional per-field spacings in Hz; ``None`` = not overridden, ``0`` = disabled."""

    carrier: int | None = None
    im3_2tx: int | None = None
    im3_3tx: int | None = None
    im5_2tx: int | None = None
    im7_2tx: int | None = None
    im5_3tx: int | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in _FIELDS)


@dataclass(frozen=True)
class RunOverride:
    """A coordination-run override: ``values`` replace fields, then ``scale`` multiplies all."""

    scale: float = 1.0
    values: PartialSpacing = PartialSpacing()

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise SpacingError(f"run override scale must be a positive number, got {self.scale!r}")


@dataclass(frozen=True)
class SpacingPreset:
    name: str
    description: str
    rules: SpacingRules


def resolve_profile_rules(
    preset: SpacingRules, overrides: PartialSpacing, run: RunOverride | None
) -> SpacingRules:
    """Effective rules for one profile: run override -> profile overrides -> preset."""
    rules = preset.with_overrides(overrides)
    if run is None:
        return rules
    rules = rules.with_overrides(run.values)
    return rules if run.scale == 1.0 else rules.scaled(run.scale)


def khz_to_hz(value: object, where: str) -> int:
    """A TOML number in kHz as whole Hz (>= 0); ``SpacingError`` naming ``where`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpacingError(f"{where}: expected a number in kHz, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise SpacingError(f"{where}: must be 0 or more kHz, got {value!r}")
    return round(value * 1000)


def hz_to_khz(hz: int) -> int | float:
    """Hz as a TOML kHz value (an int when whole kHz)."""
    return hz // 1000 if hz % 1000 == 0 else hz / 1000


def parse_partial_spacing(table: object, where: str = "[spacing]") -> PartialSpacing:
    """A ``[spacing]`` table (kHz) as overrides; unknown keys are errors."""
    if not isinstance(table, Mapping):
        raise SpacingError(f"{where}: expected a table")
    unknown = sorted(set(table) - set(_FIELDS))
    if unknown:
        raise SpacingError(f"{where}: unknown field {unknown[0]!r} (valid: {', '.join(_FIELDS)})")
    return PartialSpacing(**{k: khz_to_hz(v, f"{where} {k}") for k, v in table.items()})


def partial_to_table(partial: PartialSpacing) -> dict[str, int | float]:
    """The set fields of ``partial`` as a kHz TOML table."""
    return {f: hz_to_khz(v) for f in _FIELDS if (v := getattr(partial, f)) is not None}


def parse_preset(text: str) -> SpacingPreset:
    """Parse a spacing preset file; missing rules are disabled (0)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SpacingError(f"invalid TOML: {exc}") from exc
    return preset_from_dict(data)


def preset_from_dict(data: Mapping[str, Any]) -> SpacingPreset:
    head = data.get("preset")
    name = head.get("name") if isinstance(head, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        raise SpacingError("[preset] name: a non-empty text is required")
    description = head.get("description", "") if isinstance(head, Mapping) else ""
    if not isinstance(description, str):
        raise SpacingError(f"[preset] description: expected text, got {description!r}")
    partial = parse_partial_spacing(data.get("spacing", {}))
    rules = SpacingRules().with_overrides(partial)
    return SpacingPreset(name.strip(), description, rules)


def preset_to_toml(preset: SpacingPreset) -> str:
    """Serialise ``preset`` (kHz) as TOML text."""
    head: dict[str, Any] = {"name": preset.name}
    if preset.description:
        head["description"] = preset.description
    table = {f: hz_to_khz(getattr(preset.rules, f)) for f in _FIELDS}
    return tomli_w.dumps({"preset": head, "spacing": table})


def builtin_presets() -> dict[str, SpacingPreset]:
    """The presets shipped in the package, keyed by name."""
    out: dict[str, SpacingPreset] = {}
    for stem in spacing_presets.available():
        preset = parse_preset(spacing_presets.read_text(stem))
        out[preset.name] = preset
    return out


__all__ = [
    "PartialSpacing",
    "RunOverride",
    "SpacingError",
    "SpacingPreset",
    "SpacingRules",
    "builtin_presets",
    "hz_to_khz",
    "khz_to_hz",
    "parse_partial_spacing",
    "parse_preset",
    "partial_to_table",
    "preset_from_dict",
    "preset_to_toml",
    "resolve_profile_rules",
]
