"""Device profiles: pure parsing, validation, serialising and candidate generation.

A profile describes one device type. Exactly one frequency source is used: ``tuning`` ranges with
a ``step_khz``, a flat ``channels`` list, or ``[[groups]]`` of channels. Frequencies are ``int`` Hz
in code and MHz in TOML; spacings are kHz in TOML (see ``opencoord.coord.spacing``).
No file I/O here: see ``opencoord.io.profile_store``.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import tomli_w

from opencoord.coord import profile_templates
from opencoord.coord.spacing import (
    PartialSpacing,
    SpacingError,
    builtin_presets,
    parse_partial_spacing,
    partial_to_table,
)

Kind = Literal["mic", "iem", "other"]
KINDS: tuple[str, ...] = ("mic", "iem", "other")
MAX_CANDIDATES = 100_000


class ProfileError(ValueError):
    """Invalid profile data; the message names the field and the offending value."""


@dataclass(frozen=True)
class TuningRange:
    start_hz: int
    stop_hz: int


@dataclass(frozen=True)
class ChannelGroup:
    name: str
    channels: tuple[int, ...]


@dataclass(frozen=True)
class Candidate:
    """A candidate carrier; ``groups`` lists the groups containing it (empty if ungrouped)."""

    freq_hz: int
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    kind: Kind
    spacing_preset: str
    tuning: tuple[TuningRange, ...] = ()
    step_hz: int | None = None
    channels: tuple[int, ...] = ()
    groups: tuple[ChannelGroup, ...] = ()
    spacing_overrides: PartialSpacing = field(default_factory=PartialSpacing)


def candidates(profile: DeviceProfile) -> tuple[Candidate, ...]:
    """Candidate carrier frequencies, sorted and unique, with group membership."""
    if profile.groups:
        members: dict[int, list[str]] = {}
        for g in profile.groups:
            for f in g.channels:
                members.setdefault(f, []).append(g.name)
        return tuple(Candidate(f, tuple(members[f])) for f in sorted(members))
    if profile.channels:
        return tuple(Candidate(f) for f in sorted(set(profile.channels)))
    step = profile.step_hz
    if not step:
        return ()
    freqs: set[int] = set()
    for r in profile.tuning:
        freqs.update(range(r.start_hz, r.stop_hz + 1, step))
    return tuple(Candidate(f) for f in sorted(freqs))


def parse_profile(text: str, preset_names: Collection[str]) -> DeviceProfile:
    """Parse and validate profile TOML; ``ProfileError`` with a readable message otherwise."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"invalid TOML: {exc}") from exc
    return profile_from_dict(data, preset_names)


def _mhz_to_hz(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProfileError(f"{where}: expected a frequency in MHz, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ProfileError(f"{where}: frequency must be above 0 MHz, got {value!r}")
    return round(value * 1_000_000)


def _channel_list(value: object, where: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ProfileError(f"{where}: expected a list of MHz values, got {value!r}")
    out = [_mhz_to_hz(v, f"{where}[{i}]") for i, v in enumerate(value)]
    seen: set[int] = set()
    for hz in out:
        if hz in seen:
            raise ProfileError(f"{where}: duplicate channel {hz / 1e6:g} MHz")
        seen.add(hz)
    return tuple(out)


def profile_from_dict(data: Mapping[str, Any], preset_names: Collection[str]) -> DeviceProfile:
    head = data.get("profile")
    if not isinstance(head, Mapping):
        raise ProfileError("missing [profile] table")

    name = head.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("[profile] name: a non-empty text is required")

    kind = head.get("kind", "mic")
    if kind not in KINDS:
        raise ProfileError(f"[profile] kind: must be one of {', '.join(KINDS)}, got {kind!r}")

    preset = head.get("spacing")
    available = ", ".join(sorted(preset_names)) or "(none)"
    if not isinstance(preset, str):
        raise ProfileError(f"[profile] spacing: a preset name is required (available: {available})")
    if preset not in preset_names:
        raise ProfileError(f"[profile] spacing: unknown preset {preset!r} (available: {available})")

    raw_tuning = head.get("tuning", [])
    if not isinstance(raw_tuning, list):
        raise ProfileError(
            f"[profile] tuning: expected a list of [start, stop] MHz, got {raw_tuning!r}"
        )
    tuning: list[TuningRange] = []
    for i, item in enumerate(raw_tuning):
        where = f"[profile] tuning[{i}]"
        if not isinstance(item, list) or len(item) != 2:
            raise ProfileError(f"{where}: expected [start, stop] in MHz, got {item!r}")
        lo = _mhz_to_hz(item[0], where)
        hi = _mhz_to_hz(item[1], where)
        if lo >= hi:
            raise ProfileError(f"{where}: start must be below stop, got {item[0]!r} to {item[1]!r}")
        tuning.append(TuningRange(lo, hi))

    channels = _channel_list(head.get("channels", []), "[profile] channels")

    groups: list[ChannelGroup] = []
    raw_groups = data.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ProfileError("[[groups]]: expected an array of tables")
    for i, g in enumerate(raw_groups):
        if not isinstance(g, Mapping):
            raise ProfileError(f"[[groups]] #{i + 1}: expected a table")
        gname = g.get("name")
        if not isinstance(gname, str) or not gname.strip():
            raise ProfileError(f"[[groups]] #{i + 1} name: a non-empty text is required")
        if any(x.name == gname for x in groups):
            raise ProfileError(f"[[groups]] group {gname!r}: name is used twice")
        chans = _channel_list(g.get("channels", []), f"[[groups]] group {gname!r} channels")
        if not chans:
            raise ProfileError(f"[[groups]] group {gname!r}: needs at least one channel")
        groups.append(ChannelGroup(gname, chans))

    sources = [n for n, v in (("tuning", tuning), ("channels", channels), ("groups", groups)) if v]
    if not sources:
        raise ProfileError("[profile]: define one of tuning, channels or groups")
    if len(sources) > 1:
        raise ProfileError(
            f"[profile]: use only one of tuning, channels or groups (found {', '.join(sources)})"
        )

    step_hz: int | None = None
    step = head.get("step_khz")
    if tuning:
        if step is None:
            raise ProfileError("[profile] step_khz: required when tuning ranges are used")
        if isinstance(step, bool) or not isinstance(step, int | float):
            raise ProfileError(f"[profile] step_khz: expected a number in kHz, got {step!r}")
        if not math.isfinite(step) or round(step * 1000) <= 0:
            raise ProfileError(f"[profile] step_khz: must be above 0 kHz, got {step!r}")
        step_hz = round(step * 1000)
        count = sum((r.stop_hz - r.start_hz) // step_hz + 1 for r in tuning)
        if count > MAX_CANDIDATES:
            raise ProfileError(
                f"[profile] tuning: {count} candidates is too many (max {MAX_CANDIDATES}); "
                "increase step_khz or narrow the ranges"
            )

    try:
        overrides = parse_partial_spacing(data.get("spacing", {}), "[spacing]")
    except SpacingError as exc:
        raise ProfileError(str(exc)) from exc

    return DeviceProfile(
        name=name.strip(),
        kind=cast(Kind, kind),
        spacing_preset=preset,
        tuning=tuple(tuning),
        step_hz=step_hz,
        channels=channels,
        groups=tuple(groups),
        spacing_overrides=overrides,
    )


def _mhz(hz: int) -> int | float:
    return hz // 1_000_000 if hz % 1_000_000 == 0 else hz / 1_000_000


def profile_to_toml(profile: DeviceProfile) -> str:
    """Serialise ``profile`` as TOML text that ``parse_profile`` reads back unchanged."""
    head: dict[str, Any] = {"name": profile.name, "kind": profile.kind}
    if profile.tuning:
        head["tuning"] = [[_mhz(r.start_hz), _mhz(r.stop_hz)] for r in profile.tuning]
        if profile.step_hz is not None:
            step = profile.step_hz
            head["step_khz"] = step // 1000 if step % 1000 == 0 else step / 1000
    if profile.channels:
        head["channels"] = [_mhz(f) for f in profile.channels]
    head["spacing"] = profile.spacing_preset
    doc: dict[str, Any] = {"profile": head}
    overrides = partial_to_table(profile.spacing_overrides)
    if overrides:
        doc["spacing"] = overrides
    if profile.groups:
        doc["groups"] = [
            {"name": g.name, "channels": [_mhz(f) for f in g.channels]} for g in profile.groups
        ]
    return tomli_w.dumps(doc)


def builtin_templates() -> dict[str, DeviceProfile]:
    """Packaged profile templates keyed by file stem (e.g. ``generic-iem``)."""
    names = set(builtin_presets())
    return {
        stem: parse_profile(profile_templates.read_text(stem), names)
        for stem in profile_templates.available()
    }


__all__ = [
    "Candidate",
    "ChannelGroup",
    "DeviceProfile",
    "Kind",
    "ProfileError",
    "TuningRange",
    "builtin_templates",
    "candidates",
    "parse_profile",
    "profile_from_dict",
    "profile_to_toml",
]
