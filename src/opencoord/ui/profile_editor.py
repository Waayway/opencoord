"""Draft models and pure editing logic for device profiles and spacing presets (no Dear PyGui).

A draft is the editable form of a profile or preset. Every edit is an intent method; nothing is
validated while typing. ``build()`` / ``validate()`` convert the draft to the TOML dictionary shape
and run it through the validating parse path of ``opencoord.coord`` (``profile_from_dict`` /
``preset_from_dict``), so an editor can never produce a profile the loader would reject.

Forgiving by design: a profile keeps the data of every frequency source (tuning, channels, groups)
and only the chosen mode is saved (``dropped_warning()`` says what is left out), channel lists are
free text that accepts newline / comma / semicolon / space / tab separated MHz values, duplicates
are merged and the list is sorted, and each bad token is reported on its own.

``structure_version`` changes when widgets must be rebuilt (rows added or removed, other profile
loaded); ``revision`` changes on every edit (texts, counters and buttons refresh).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from opencoord.coord.profiles import (
    KINDS,
    MAX_MHZ,
    DeviceProfile,
    Kind,
    ProfileError,
    candidates,
    profile_from_dict,
)
from opencoord.coord.spacing import (
    PartialSpacing,
    SpacingError,
    SpacingPreset,
    SpacingRules,
    hz_to_khz,
    khz_to_hz,
    preset_from_dict,
    resolve_profile_rules,
)

Mode = Literal["tuning", "channels", "groups"]
MODES: tuple[Mode, ...] = ("tuning", "channels", "groups")
MODE_LABELS: dict[str, str] = {
    "tuning": "Tuning ranges + step",
    "channels": "Channel list",
    "groups": "Groups (banks) of channels",
}
SPACING_FIELDS: tuple[str, ...] = SpacingRules.FIELDS
SPACING_LABELS: dict[str, str] = {
    "carrier": "Carrier spacing",
    "im3_2tx": "3rd order, 2 transmitters",
    "im3_3tx": "3rd order, 3 transmitters",
    "im5_2tx": "5th order, 2 transmitters",
    "im7_2tx": "7th order, 2 transmitters",
    "im5_3tx": "5th order, 3 transmitters (advanced)",
}
DEFAULT_STEP_KHZ = 25.0
_SPLIT = re.compile(r"[;\s]+")
_DECIMAL_COMMA = re.compile(r"\d+,\d+")
_BAD_NUMBER = "is not a number (use MHz, e.g. 470.125 or 470,125)"
_AMBIGUOUS = "is ambiguous: use '.' or ',' for decimals and '; ' or new lines between values"
_HZ_PER_MHZ = 1_000_000
_UIDS = itertools.count(1)


# --- helpers ---------------------------------------------------------------------------------


def format_mhz(hz: int) -> str:
    """Exact MHz text of ``hz`` without trailing zeros (``470125000`` -> ``470.125``)."""
    text = f"{Decimal(hz) / _HZ_PER_MHZ:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_khz(value: float) -> str:
    return f"{value:g}"


def unique_name(base: str, taken: Collection[str]) -> str:
    """``base`` if free, else ``base 2``, ``base 3``, ..."""
    base = base.strip() or "Unnamed"
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


_FRIENDLY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[profile\] step_khz"), "Step"),
    (re.compile(r"\[profile\] tuning"), "Tuning"),
    (re.compile(r"\[profile\] channels"), "Channels"),
    (re.compile(r"\[profile\] spacing"), "Spacing preset"),
    (re.compile(r"\[profile\] name"), "Name"),
    (re.compile(r"\[profile\] kind"), "Kind"),
    (re.compile(r"\[\[groups\]\] #(\d+) name"), "Group {} name"),
    (re.compile(r"\[\[groups\]\] #(\d+)"), "Group {}"),
    (re.compile(r"\[\[groups\]\] group"), "Group"),
    (re.compile(r"\[preset\] name"), "Name"),
    (re.compile(r"\[preset\] description"), "Description"),
)
_TUNING_REF = re.compile(r"\[profile\] tuning\[(\d+)\]")
_SPACING_REF = re.compile(r"\[spacing\] (\w+)")


def friendly(message: str) -> str:
    """A parser message without TOML table names (``[profile] step_khz`` -> ``Step``)."""
    message = _TUNING_REF.sub(lambda m: f"Tuning range {int(m.group(1)) + 1}", message)
    message = _SPACING_REF.sub(lambda m: SPACING_LABELS.get(m.group(1), m.group(1)), message)
    for pattern, template in _FRIENDLY:
        message = _substitute(pattern, template, message)
    return message


def _substitute(pattern: re.Pattern[str], template: str, message: str) -> str:
    return pattern.sub(lambda m: template.format(*m.groups()), message)


# --- pasted channel lists --------------------------------------------------------------------


@dataclass(frozen=True)
class PasteResult:
    """Outcome of parsing a pasted channel list: sorted unique Hz values plus per-token errors."""

    values_hz: tuple[int, ...]
    errors: tuple[str, ...] = ()
    duplicates: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_channel_text(text: str) -> PasteResult:
    """MHz values separated by newlines, semicolons, tabs, spaces or a comma plus space.

    A comma directly between digits is a decimal separator (``470,125`` = 470.125 MHz). A token
    that mixes dots and commas or has several commas (``470,125,470,250``) is ambiguous and gives
    an error. A trailing ``MHz`` is accepted. Each bad token gives one error; good tokens are
    still returned, sorted and de-duplicated.
    """
    seen: set[int] = set()
    errors: list[str] = []
    duplicates = 0
    for piece in _SPLIT.split(text.strip()):
        raw = piece.strip(",")  # a comma followed by white space separates values
        if not raw or raw.lower() == "mhz":
            continue
        token = raw[:-3] if raw.lower().endswith("mhz") else raw
        if "," in token:
            if token.count(",") > 1 or "." in token:
                errors.append(f"{raw!r} {_AMBIGUOUS}")
                continue
            if _DECIMAL_COMMA.fullmatch(token):
                token = token.replace(",", ".")
        try:
            value = Decimal(token)
        except InvalidOperation:
            errors.append(f"{raw!r} {_BAD_NUMBER}")
            continue
        if not value.is_finite():
            errors.append(f"{raw!r} {_BAD_NUMBER}")
        elif value <= 0:
            errors.append(f"{raw!r} must be above 0 MHz")
        elif value > MAX_MHZ:
            errors.append(f"{raw!r} is above the limit of {MAX_MHZ:g} MHz")
        else:
            hz = int((value * _HZ_PER_MHZ).to_integral_value())
            if hz <= 0:
                errors.append(f"{raw!r} is below 1 Hz")
            elif hz in seen:
                duplicates += 1
            else:
                seen.add(hz)
    return PasteResult(tuple(sorted(seen)), tuple(errors), duplicates)


@dataclass
class ChannelList:
    """Free text plus what it parses to; the text is kept as typed."""

    text: str = ""
    values: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()
    duplicates: int = 0

    def set_text(self, text: str) -> PasteResult:
        result = parse_channel_text(text)
        self.text = text
        self.values = result.values_hz
        self.errors = result.errors
        self.duplicates = result.duplicates
        return result

    def set_values(self, values: Iterable[int]) -> None:
        self.set_text("\n".join(format_mhz(v) for v in sorted(set(values))))

    def tidy(self) -> bool:
        """Rewrite sorted and de-duplicated, one MHz per line; ``False`` if there are errors."""
        if self.errors:
            return False
        self.set_values(self.values)
        return True


# --- profile draft ---------------------------------------------------------------------------


@dataclass
class RangeDraft:
    start_mhz: float = 0.0
    stop_mhz: float = 0.0


@dataclass
class GroupDraft:
    name: str = ""
    channels: ChannelList = field(default_factory=ChannelList)


@dataclass(frozen=True)
class Preview:
    """What a valid draft offers the solver: candidate count, span and the count per group."""

    count: int
    low_hz: int | None
    high_hz: int | None
    per_group: tuple[tuple[str, int], ...] = ()

    @property
    def text(self) -> str:
        if self.count == 0 or self.low_hz is None or self.high_hz is None:
            return "No candidate frequencies"
        span = f"{format_mhz(self.low_hz)} - {format_mhz(self.high_hz)} MHz"
        noun = "frequency" if self.count == 1 else "frequencies"
        return f"{self.count} candidate {noun}, {span}"


@dataclass(frozen=True)
class EffectiveField:
    """One spacing rule as the profile will use it (kHz): preset value or its override."""

    name: str
    label: str
    preset_khz: float | None
    effective_khz: float | None
    overridden: bool


class ProfileDraft:
    def __init__(self) -> None:
        #: Unique per draft object, so a view can tell "another draft" from "an edit".
        self.uid = next(_UIDS)
        self.name = ""
        self.kind: Kind = "mic"
        self.preset = ""
        self.mode: Mode = "tuning"
        self.ranges: list[RangeDraft] = []
        self.step_khz = DEFAULT_STEP_KHZ
        self.channels = ChannelList()
        self.groups: list[GroupDraft] = []
        self.overrides: dict[str, float | None] = dict.fromkeys(SPACING_FIELDS)
        #: Name of the stored profile this draft was loaded from (``None``: not saved yet).
        self.original_name: str | None = None
        self.structure_version = 0
        self.revision = 0
        self._baseline: tuple[Any, ...] = ()
        self._preview_cache: tuple[int, tuple[str, ...], Preview | None] | None = None
        self.mark_saved(self.original_name)

    # --- creation ---

    @classmethod
    def blank(cls, preset: str = "") -> ProfileDraft:
        draft = cls()
        draft.name = "New profile"
        draft.preset = preset
        draft.ranges = [RangeDraft()]
        draft.mark_saved(None)
        return draft

    @classmethod
    def from_profile(cls, profile: DeviceProfile, original_name: str | None) -> ProfileDraft:
        draft = cls()
        draft.name = profile.name
        draft.kind = profile.kind
        draft.preset = profile.spacing_preset
        draft.ranges = [
            RangeDraft(r.start_hz / _HZ_PER_MHZ, r.stop_hz / _HZ_PER_MHZ) for r in profile.tuning
        ]
        if profile.step_hz is not None:
            draft.step_khz = profile.step_hz / 1000
        draft.channels.set_values(profile.channels)
        draft.groups = []
        for g in profile.groups:
            group = GroupDraft(g.name)
            group.channels.set_values(g.channels)
            draft.groups.append(group)
        for name in SPACING_FIELDS:
            value = getattr(profile.spacing_overrides, name)
            draft.overrides[name] = None if value is None else float(hz_to_khz(value))
        draft.mode = "groups" if profile.groups else "channels" if profile.channels else "tuning"
        draft.mark_saved(original_name)
        return draft

    def clone(self, taken_names: Collection[str]) -> ProfileDraft:
        """A new unsaved draft with the same content (including unsaved edits)."""
        other = ProfileDraft()
        other.name = unique_name(f"{self.name.strip() or 'Profile'} copy", taken_names)
        other.kind = self.kind
        other.preset = self.preset
        other.mode = self.mode
        other.ranges = [RangeDraft(r.start_mhz, r.stop_mhz) for r in self.ranges]
        other.step_khz = self.step_khz
        other.channels.set_text(self.channels.text)
        other.groups = []
        for g in self.groups:
            group = GroupDraft(g.name)
            group.channels.set_text(g.channels.text)
            other.groups.append(group)
        other.overrides = dict(self.overrides)
        other.mark_saved(None)
        other.treat_as_edited()  # a clone is worth a discard prompt
        return other

    # --- change tracking ---

    def _touch(self, structure: bool = False) -> None:
        self.revision += 1
        if structure:
            self.structure_version += 1

    def _snapshot(self) -> tuple[Any, ...]:
        return (
            self.name,
            self.kind,
            self.preset,
            self.mode,
            tuple((r.start_mhz, r.stop_mhz) for r in self.ranges),
            self.step_khz,
            self.channels.text,
            tuple((g.name, g.channels.text) for g in self.groups),
            tuple(self.overrides.items()),
        )

    def mark_saved(self, original_name: str | None) -> None:
        self.original_name = original_name
        self._baseline = self._snapshot()
        self._touch()

    def treat_as_edited(self) -> None:
        """Mark the content as unsaved work (compares unequal to the saved state)."""
        self._baseline = ()

    @property
    def is_new(self) -> bool:
        return self.original_name is None

    @property
    def dirty(self) -> bool:
        """Edited since it was created, loaded or saved (clones count as edited)."""
        return self._snapshot() != self._baseline

    @property
    def unsaved(self) -> bool:
        """Not stored yet, or edited since the last load or save."""
        return self.is_new or self.dirty

    # --- simple fields ---

    def set_name(self, name: str) -> None:
        self.name = name
        self._touch()

    def set_kind(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        self.kind = cast(Kind, kind)
        self._touch()

    def set_preset(self, preset: str) -> None:
        self.preset = preset
        self._touch()

    def set_step_khz(self, value: float) -> None:
        self.step_khz = value
        self._touch()

    # --- tuning ranges ---

    def add_range(self, start_mhz: float = 0.0, stop_mhz: float = 0.0) -> int:
        self.ranges.append(RangeDraft(start_mhz, stop_mhz))
        self._touch(structure=True)
        return len(self.ranges) - 1

    def remove_range(self, index: int) -> None:
        del self.ranges[index]
        self._touch(structure=True)

    def set_range(self, index: int, start_mhz: float, stop_mhz: float) -> None:
        self.ranges[index] = RangeDraft(start_mhz, stop_mhz)
        self._touch()

    # --- channels and groups ---

    def paste_channels(
        self, text: str, group: int | None = None, *, append: bool = False
    ) -> PasteResult:
        """Set (or, with ``append``, extend) a channel list from free text.

        ``group`` is the group index, ``None`` the flat channel list. Per-token errors are kept in
        the list and reported by ``validate()``; the good values are still parsed, sorted and
        de-duplicated into ``values``.
        """
        target = self.channels if group is None else self.groups[group].channels
        if append and target.text.strip():
            text = f"{target.text.rstrip()}\n{text}"
        result = target.set_text(text)
        self._touch()
        return result

    def tidy_channels(self, group: int | None = None) -> bool:
        target = self.channels if group is None else self.groups[group].channels
        done = target.tidy()
        self._touch()
        return done

    def add_group(self, name: str | None = None) -> int:
        label = name if name is not None else unique_name("Group", [g.name for g in self.groups])
        self.groups.append(GroupDraft(label))
        self._touch(structure=True)
        return len(self.groups) - 1

    def remove_group(self, index: int) -> None:
        del self.groups[index]
        self._touch(structure=True)

    def rename_group(self, index: int, name: str) -> None:
        self.groups[index].name = name
        self._touch()

    # --- source mode ---

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self._touch(structure=True)

    def _other_data(self) -> list[str]:
        out: list[str] = []
        if self.mode != "tuning" and self.ranges:
            out.append(f"{len(self.ranges)} tuning range{'s' if len(self.ranges) != 1 else ''}")
        if self.mode != "channels" and self.channels.values:
            out.append(f"{len(self.channels.values)} channels")
        if self.mode != "groups" and self.groups:
            out.append(f"{len(self.groups)} group{'s' if len(self.groups) != 1 else ''}")
        return out

    def dropped_warning(self) -> str | None:
        """What saving leaves out because it belongs to a frequency source that is not selected."""
        other = self._other_data()
        if not other:
            return None
        return (
            f"Mode '{MODE_LABELS[self.mode]}' is selected. Saving drops: {', '.join(other)} "
            "(kept here until you save or switch back)."
        )

    # --- overrides ---

    def set_override(self, name: str, khz: float) -> None:
        if name not in SPACING_FIELDS:
            raise ValueError(f"unknown spacing field {name!r}")
        self.overrides[name] = khz
        self._touch()

    def clear_override(self, name: str) -> None:
        if name not in SPACING_FIELDS:
            raise ValueError(f"unknown spacing field {name!r}")
        self.overrides[name] = None
        self._touch()

    def effective_spacing(self, preset_rules: SpacingRules | None) -> list[EffectiveField]:
        """Per rule: the preset value and the value this profile uses (kHz)."""
        partial: dict[str, int] = {}
        for name, khz in self.overrides.items():
            if khz is not None:
                try:
                    partial[name] = khz_to_hz(khz, name)
                except SpacingError:
                    continue  # reported by validate(); shown as not overridden meanwhile
        effective = (
            None
            if preset_rules is None
            else resolve_profile_rules(preset_rules, PartialSpacing(**partial), None)
        )
        out: list[EffectiveField] = []
        for name in SPACING_FIELDS:
            base = None if preset_rules is None else float(hz_to_khz(getattr(preset_rules, name)))
            value = None if effective is None else float(hz_to_khz(getattr(effective, name)))
            out.append(EffectiveField(name, SPACING_LABELS[name], base, value, name in partial))
        return out

    # --- validation ---

    def _active_list_errors(self) -> list[str]:
        if self.mode == "channels":
            return [f"Channels: {e}" for e in self.channels.errors]
        if self.mode == "groups":
            return [
                f"Group {g.name.strip() or i + 1}: {e}"
                for i, g in enumerate(self.groups)
                for e in g.channels.errors
            ]
        return []

    def to_dict(self) -> dict[str, Any]:
        """The draft in the TOML dictionary shape (MHz / kHz), only the selected mode's data."""
        head: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "spacing": self.preset,
        }
        doc: dict[str, Any] = {"profile": head}
        if self.mode == "tuning":
            head["tuning"] = [[r.start_mhz, r.stop_mhz] for r in self.ranges]
            head["step_khz"] = self.step_khz
        elif self.mode == "channels":
            head["channels"] = [v / _HZ_PER_MHZ for v in self.channels.values]
        else:
            doc["groups"] = [
                {"name": g.name.strip(), "channels": [v / _HZ_PER_MHZ for v in g.channels.values]}
                for g in self.groups
            ]
        overrides = {k: v for k, v in self.overrides.items() if v is not None}
        if overrides:
            doc["spacing"] = overrides
        return doc

    def build(
        self, preset_names: Collection[str], taken_names: Collection[str] = ()
    ) -> tuple[DeviceProfile | None, list[str]]:
        """Run the draft through the validating parse path: ``(profile, [])`` or ``(None, errors)``.

        ``taken_names`` are the names of other stored profiles (a clash is an error).
        """
        errors = self._active_list_errors()
        name = self.name.strip()
        if name and name in taken_names:
            errors.append(f"A profile named '{name}' already exists; choose another name")
        empty = {
            "tuning": ("tuning", "Add at least one tuning range"),
            "channels": ("channels", "Paste at least one channel (MHz)"),
            "groups": ("groups", "Add at least one group with channels"),
        }[self.mode]
        profile: DeviceProfile | None = None
        try:
            profile = profile_from_dict(self.to_dict(), preset_names)
        except ProfileError as exc:
            message = str(exc)
            if "define one of tuning" in message:
                errors.append(empty[1])
            elif errors and "needs at least one channel" in message:
                pass  # the pasted text has bad tokens: already reported
            else:
                errors.append(friendly(message))
        if errors:
            return None, errors
        return profile, []

    def validate(
        self, preset_names: Collection[str], taken_names: Collection[str] = ()
    ) -> list[str]:
        return self.build(preset_names, taken_names)[1]

    def preview(self, preset_names: Collection[str]) -> Preview | None:
        """Candidate count and span of the draft, ``None`` while it is invalid."""
        key = (self.revision, tuple(sorted(preset_names)))
        if self._preview_cache is not None and self._preview_cache[:2] == key:
            return self._preview_cache[2]
        profile, _ = self.build(preset_names)
        result: Preview | None = None
        if profile is not None:
            cands = candidates(profile)
            per_group = tuple((g.name, len(g.channels)) for g in profile.groups)
            result = Preview(
                len(cands),
                cands[0].freq_hz if cands else None,
                cands[-1].freq_hz if cands else None,
                per_group,
            )
        self._preview_cache = (key[0], key[1], result)
        return result


# --- spacing preset draft --------------------------------------------------------------------


class PresetDraft:
    """Editable spacing preset: name, description and the six rules in kHz (0 = off)."""

    def __init__(self) -> None:
        self.uid = next(_UIDS)
        self.name = ""
        self.description = ""
        self.values: dict[str, float] = dict.fromkeys(SPACING_FIELDS, 0.0)
        self.original_name: str | None = None
        self.revision = 0
        self._baseline: tuple[Any, ...] = ()
        self.mark_saved(None)

    @classmethod
    def from_preset(cls, preset: SpacingPreset, original_name: str | None) -> PresetDraft:
        draft = cls()
        draft.name = preset.name
        draft.description = preset.description
        for name in SPACING_FIELDS:
            draft.values[name] = float(hz_to_khz(getattr(preset.rules, name)))
        draft.mark_saved(original_name)
        return draft

    @classmethod
    def blank(cls, base: SpacingPreset | None, taken_names: Collection[str]) -> PresetDraft:
        """A new unsaved preset, starting from ``base``'s values when given."""
        draft = cls.from_preset(base, None) if base is not None else cls()
        draft.name = unique_name("New preset", taken_names)
        draft.description = ""
        draft.mark_saved(None)
        draft.treat_as_edited()  # a copy of another preset: worth a discard prompt
        return draft

    def clone(self, taken_names: Collection[str]) -> PresetDraft:
        other = PresetDraft()
        other.name = unique_name(f"{self.name.strip() or 'Preset'} copy", taken_names)
        other.description = self.description
        other.values = dict(self.values)
        other.mark_saved(None)
        other.treat_as_edited()
        return other

    def _snapshot(self) -> tuple[Any, ...]:
        return (self.name, self.description, tuple(self.values.items()))

    def mark_saved(self, original_name: str | None) -> None:
        self.original_name = original_name
        self._baseline = self._snapshot()
        self.revision += 1

    def treat_as_edited(self) -> None:
        """Mark the content as unsaved work (compares unequal to the saved state)."""
        self._baseline = ()

    @property
    def is_new(self) -> bool:
        return self.original_name is None

    @property
    def dirty(self) -> bool:
        return self._snapshot() != self._baseline

    @property
    def unsaved(self) -> bool:
        return self.is_new or self.dirty

    def set_name(self, name: str) -> None:
        self.name = name
        self.revision += 1

    def set_description(self, text: str) -> None:
        self.description = text
        self.revision += 1

    def set_value(self, name: str, khz: float) -> None:
        if name not in SPACING_FIELDS:
            raise ValueError(f"unknown spacing field {name!r}")
        self.values[name] = khz
        self.revision += 1

    def to_dict(self) -> dict[str, Any]:
        head: dict[str, Any] = {"name": self.name}
        if self.description:
            head["description"] = self.description
        return {"preset": head, "spacing": dict(self.values)}

    def build(self, taken_names: Collection[str] = ()) -> tuple[SpacingPreset | None, list[str]]:
        errors: list[str] = []
        name = self.name.strip()
        if name and name in taken_names:
            errors.append(f"A preset named '{name}' already exists; choose another name")
        preset: SpacingPreset | None = None
        try:
            preset = preset_from_dict(self.to_dict())
        except SpacingError as exc:
            errors.append(friendly(str(exc)))
        if errors:
            return None, errors
        return preset, []

    def validate(self, taken_names: Collection[str] = ()) -> list[str]:
        return self.build(taken_names)[1]


__all__ = [
    "MODES",
    "MODE_LABELS",
    "SPACING_FIELDS",
    "SPACING_LABELS",
    "ChannelList",
    "EffectiveField",
    "GroupDraft",
    "Mode",
    "PasteResult",
    "PresetDraft",
    "Preview",
    "ProfileDraft",
    "RangeDraft",
    "format_khz",
    "format_mhz",
    "friendly",
    "parse_channel_text",
    "unique_name",
]
