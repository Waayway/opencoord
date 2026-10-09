"""Coordination tab state (no Dear PyGui): the setup the user edits and how it becomes a request.

:class:`CoordinationModel` holds the device rows (profile name + quantity, plus the frequencies
typed for check mode), the locked carriers (MHz, label, spacing preset for their rules) and the
run options. ``revision`` changes on every edit and ``structure_version`` when rows are added or
removed, so the panel only rebuilds its row widgets when it has to.

:func:`build_request` turns the setup plus the app context (loaded profiles and presets, the main
scan trace, exclusion zones, channel plan) into a :class:`~opencoord.coord.solver.
CoordinationRequest`; :func:`check_input` does the same for a hand-made plan. Both raise
``ValueError`` with a user-facing message.

The setup (``to_dict``/``from_dict``) and a solved plan (:func:`result_to_dict` /
:func:`result_from_dict`) are plain JSON-ready dicts stored in the session file.
"""

from __future__ import annotations

import json
import math
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast, get_args

from opencoord.coord.channel_plans import ChannelPlan
from opencoord.coord.profiles import DeviceProfile
from opencoord.coord.solver import (
    MAX_DEVICES,
    Assignment,
    CoordinationRequest,
    LockedCarrier,
    Plan,
    Reason,
    SolveStats,
    Unassigned,
    Violation,
)
from opencoord.coord.spacing import PartialSpacing, RunOverride, SpacingRules
from opencoord.core.types import ExclusionZone, Trace
from opencoord.ui.profile_editor import (
    SPACING_FIELDS,
    PasteResult,
    format_mhz,
    parse_channel_text,
)

MAX_DEVICE_ROWS: Final = 32
MAX_LOCKS: Final = 64
DEFAULT_LOCK_PRESET: Final = "generic-analog"

#: ``(min, max)`` accepted by the option setters (values outside are clamped).
THRESHOLD_DB_LIMITS: Final = (0.0, 60.0)
GUARD_KHZ_LIMITS: Final = (0.0, 5000.0)
TIME_BUDGET_S_LIMITS: Final = (0.5, 300.0)
BACKUPS_LIMITS: Final = (0, 10)
SCALE_LIMITS: Final = (0.1, 10.0)
OVERRIDE_KHZ_LIMITS: Final = (0.0, 100_000.0)


def _clamp(value: float, limits: tuple[float, float]) -> float:
    lo, hi = limits
    if not math.isfinite(value):
        return lo
    return min(max(float(value), lo), hi)


def lock_label(freq_hz: int) -> str:
    return f"Locked {format_mhz(freq_hz)}"


@dataclass
class DeviceRow:
    profile: str
    quantity: int = 1
    #: Frequencies typed or pasted for check mode (MHz text, as typed).
    check_text: str = ""


@dataclass(frozen=True)
class LockRow:
    """A locked carrier: frequency, label and the spacing preset its rules come from."""

    freq_hz: int
    label: str
    preset: str = DEFAULT_LOCK_PRESET


@dataclass
class Options:
    use_scan: bool = True
    threshold_db: float = 10.0
    guard_khz: float = 100.0
    allow_forbidden: bool = False
    prefer_single_group: bool = True
    time_budget_s: float = 5.0
    backups_per_profile: int = 2
    override_enabled: bool = False
    override_scale: float = 1.0
    #: Run-override spacings in kHz for the fields that are overridden.
    override_khz: dict[str, float] = field(default_factory=dict)


class CoordinationModel:
    def __init__(self) -> None:
        self.rows: list[DeviceRow] = []
        self.locks: list[LockRow] = []
        self.options = Options()
        self.revision = 0
        self.structure_version = 0

    def _touch(self, structure: bool = False) -> None:
        self.revision += 1
        if structure:
            self.structure_version += 1

    # --- device rows ---------------------------------------------------------------------------

    def add_device(self, profile: str = "", quantity: int = 1) -> int | None:
        """Add a row; its index, or ``None`` at ``MAX_DEVICE_ROWS``."""
        if len(self.rows) >= MAX_DEVICE_ROWS:
            return None
        self.rows.append(DeviceRow(profile, _quantity(quantity)))
        self._touch(structure=True)
        return len(self.rows) - 1

    def remove_device(self, index: int) -> None:
        if 0 <= index < len(self.rows):
            del self.rows[index]
            self._touch(structure=True)

    def set_device_profile(self, index: int, name: str) -> None:
        if 0 <= index < len(self.rows):
            self.rows[index].profile = name
            self._touch()

    def set_quantity(self, index: int, quantity: int) -> None:
        if 0 <= index < len(self.rows):
            self.rows[index].quantity = _quantity(quantity)
            self._touch()

    def set_check_text(self, index: int, text: str) -> None:
        if 0 <= index < len(self.rows):
            self.rows[index].check_text = text
            self._touch()

    def fill_check(self, plan: Plan) -> None:
        """Put the plan's frequencies into the check boxes (split over rows of one profile)."""
        by_profile: dict[str, list[int]] = {}
        for a in plan.assignments:
            by_profile.setdefault(a.profile_name, []).append(a.freq_hz)
        for freqs in by_profile.values():
            freqs.sort()
        last_row = {row.profile: i for i, row in enumerate(self.rows)}
        for i, row in enumerate(self.rows):
            freqs = by_profile.get(row.profile, [])
            take = len(freqs) if last_row[row.profile] == i else row.quantity
            row.check_text = "; ".join(format_mhz(f) for f in freqs[:take])
            del freqs[:take]
        self._touch(structure=True)

    # --- locked carriers -----------------------------------------------------------------------

    def paste_locks(
        self, text: str, label: str = "", preset: str = DEFAULT_LOCK_PRESET
    ) -> PasteResult:
        """Lock every MHz value in ``text`` (same rules as the profile channel lists).

        Values already locked are skipped. Several values with one ``label`` are numbered.
        """
        result = parse_channel_text(text)
        taken = {lk.freq_hz for lk in self.locks}
        new = [f for f in result.values_hz if f not in taken]
        errors = list(result.errors)
        room = MAX_LOCKS - len(self.locks)
        if len(new) > room:
            errors.append(f"There can be at most {MAX_LOCKS} locked carriers")
            new = new[: max(room, 0)]
        label = label.strip()
        for n, f in enumerate(new, 1):
            name = (f"{label} {n}" if len(new) > 1 else label) if label else lock_label(f)
            self.locks.append(LockRow(f, name, preset))
        self.locks.sort(key=lambda lk: lk.freq_hz)
        if new:
            self._touch(structure=True)
        return replace(result, errors=tuple(errors))

    def remove_lock(self, index: int) -> None:
        if 0 <= index < len(self.locks):
            del self.locks[index]
            self._touch(structure=True)

    def clear_locks(self) -> None:
        if self.locks:
            self.locks = []
            self._touch(structure=True)

    def set_lock_label(self, index: int, label: str) -> None:
        if 0 <= index < len(self.locks):
            lk = self.locks[index]
            self.locks[index] = replace(lk, label=label.strip() or lock_label(lk.freq_hz))
            self._touch()

    def set_lock_preset(self, index: int, preset: str) -> None:
        if 0 <= index < len(self.locks):
            self.locks[index] = replace(self.locks[index], preset=preset)
            self._touch()

    # --- options -------------------------------------------------------------------------------

    def _set(self, **changes: Any) -> None:
        self.options = replace(self.options, **changes)
        self._touch()

    def set_use_scan(self, on: bool) -> None:
        self._set(use_scan=bool(on))

    def set_threshold_db(self, value: float) -> None:
        self._set(threshold_db=_clamp(value, THRESHOLD_DB_LIMITS))

    def set_guard_khz(self, value: float) -> None:
        self._set(guard_khz=_clamp(value, GUARD_KHZ_LIMITS))

    def set_allow_forbidden(self, on: bool) -> None:
        self._set(allow_forbidden=bool(on))

    def set_prefer_single_group(self, on: bool) -> None:
        self._set(prefer_single_group=bool(on))

    def set_time_budget_s(self, value: float) -> None:
        self._set(time_budget_s=_clamp(value, TIME_BUDGET_S_LIMITS))

    def set_backups(self, value: int) -> None:
        lo, hi = BACKUPS_LIMITS
        self._set(backups_per_profile=min(max(int(value), lo), hi))

    def set_override_enabled(self, on: bool) -> None:
        self._set(override_enabled=bool(on))

    def set_override_scale(self, value: float) -> None:
        self._set(override_scale=_clamp(value, SCALE_LIMITS))

    def set_override_value(self, name: str, khz: float | None) -> None:
        """Override one spacing field (kHz) for this run; ``None`` removes the override."""
        if name not in SPACING_FIELDS:
            raise KeyError(name)
        values = dict(self.options.override_khz)
        if khz is None:
            values.pop(name, None)
        else:
            values[name] = _clamp(khz, OVERRIDE_KHZ_LIMITS)
        self._set(override_khz=values)

    def solve_key(self) -> str:
        """Everything that affects a coordination run (check-mode texts excluded), as text."""
        data = self.to_dict()
        for row in data["devices"]:
            del row["check"]
        return json.dumps(data, sort_keys=True)

    # --- persistence ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        o = self.options
        return {
            "devices": [
                {"profile": r.profile, "quantity": r.quantity, "check": r.check_text}
                for r in self.rows
            ],
            "locked": [
                {"freq_hz": lk.freq_hz, "label": lk.label, "preset": lk.preset} for lk in self.locks
            ],
            "options": {
                "use_scan": o.use_scan,
                "threshold_db": o.threshold_db,
                "guard_khz": o.guard_khz,
                "allow_forbidden": o.allow_forbidden,
                "prefer_single_group": o.prefer_single_group,
                "time_budget_s": o.time_budget_s,
                "backups_per_profile": o.backups_per_profile,
                "override": {
                    "enabled": o.override_enabled,
                    "scale": o.override_scale,
                    "values_khz": dict(o.override_khz),
                },
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CoordinationModel:
        """A model from :meth:`to_dict` output; ``ValueError`` on wrong types. Missing fields
        take their defaults; numbers out of range are clamped."""
        m = cls()
        for rec in _records(data, "devices")[:MAX_DEVICE_ROWS]:
            row = DeviceRow(
                _typed(rec, "profile", str, ""),
                _quantity(_typed(rec, "quantity", int, 1)),
                _typed(rec, "check", str, ""),
            )
            m.rows.append(row)
        for rec in _records(data, "locked")[:MAX_LOCKS]:
            freq = _typed(rec, "freq_hz", int, None)
            if freq is None:
                raise ValueError("a locked carrier needs 'freq_hz'")
            _check_freq(freq)
            label = _typed(rec, "label", str, "").strip() or lock_label(freq)
            m.locks.append(LockRow(freq, label, _typed(rec, "preset", str, DEFAULT_LOCK_PRESET)))
        m.locks.sort(key=lambda lk: lk.freq_hz)
        raw = _typed(data, "options", dict, {})
        d = Options()
        override = _typed(raw, "override", dict, {})
        values: dict[str, float] = {}
        for name, value in _typed(override, "values_khz", dict, {}).items():
            if name not in SPACING_FIELDS or not _is_number(value):
                raise ValueError(f"invalid run override {name!r}")
            values[name] = _clamp(value, OVERRIDE_KHZ_LIMITS)
        m.options = Options(
            use_scan=_typed(raw, "use_scan", bool, d.use_scan),
            threshold_db=_clamp(_number(raw, "threshold_db", d.threshold_db), THRESHOLD_DB_LIMITS),
            guard_khz=_clamp(_number(raw, "guard_khz", d.guard_khz), GUARD_KHZ_LIMITS),
            allow_forbidden=_typed(raw, "allow_forbidden", bool, d.allow_forbidden),
            prefer_single_group=_typed(raw, "prefer_single_group", bool, d.prefer_single_group),
            time_budget_s=_clamp(
                _number(raw, "time_budget_s", d.time_budget_s), TIME_BUDGET_S_LIMITS
            ),
            backups_per_profile=int(
                _clamp(_typed(raw, "backups_per_profile", int, 2), BACKUPS_LIMITS)
            ),
            override_enabled=_typed(override, "enabled", bool, False),
            override_scale=_clamp(_number(override, "scale", 1.0), SCALE_LIMITS),
            override_khz=values,
        )
        return m


def _quantity(value: int) -> int:
    return min(max(int(value), 0), MAX_DEVICES)


# --- requests ---------------------------------------------------------------------------------


def run_override(options: Options) -> RunOverride | None:
    """The run override of ``options`` (``None`` while switched off)."""
    if not options.override_enabled:
        return None
    values = PartialSpacing(**{k: round(v * 1000) for k, v in options.override_khz.items()})
    return RunOverride(options.override_scale, values)


def _profile_for(row: DeviceRow, n: int, profiles: Mapping[str, DeviceProfile]) -> DeviceProfile:
    if not row.profile:
        raise ValueError(f"Choose a profile for device row {n}")
    profile = profiles.get(row.profile)
    if profile is None:
        raise ValueError(
            f"Profile '{row.profile}' is not loaded (device row {n}); pick another one"
        )
    return profile


def _locked(model: CoordinationModel, presets: Mapping[str, SpacingRules]) -> list[LockedCarrier]:
    out = []
    for lk in model.locks:
        rules = presets.get(lk.preset)
        if rules is None:
            raise ValueError(f"Locked carrier {lk.label}: unknown spacing preset '{lk.preset}'")
        out.append(LockedCarrier(lk.freq_hz, lk.label, rules))
    return out


def _request(
    model: CoordinationModel,
    devices: Sequence[tuple[DeviceProfile, int]],
    *,
    presets: Mapping[str, SpacingRules],
    scan: Trace | None,
    zones: Sequence[ExclusionZone],
    channel_plan: ChannelPlan | None,
) -> CoordinationRequest:
    o = model.options
    return CoordinationRequest(
        devices=devices,
        locked=_locked(model, presets),
        scan=scan if o.use_scan else None,
        zones=tuple(zones),
        channel_plan=channel_plan,
        allow_forbidden=o.allow_forbidden,
        threshold_db=o.threshold_db,
        guard_hz=round(o.guard_khz * 1000),
        prefer_single_group=o.prefer_single_group,
        time_budget_s=o.time_budget_s,
        run_override=run_override(o),
        backups_per_profile=o.backups_per_profile,
        presets=dict(presets),
    )


def _merged(items: Iterable[tuple[DeviceProfile, int]]) -> list[tuple[DeviceProfile, int]]:
    """Quantities of rows with the same profile added up (first row's position)."""
    out: dict[str, tuple[DeviceProfile, int]] = {}
    for p, q in items:
        prev = out.get(p.name)
        out[p.name] = (p, q + (prev[1] if prev else 0))
    return list(out.values())


def build_request(
    model: CoordinationModel,
    *,
    profiles: Mapping[str, DeviceProfile],
    presets: Mapping[str, SpacingRules],
    scan: Trace | None,
    zones: Sequence[ExclusionZone],
    channel_plan: ChannelPlan | None,
) -> CoordinationRequest:
    """The solver request for the setup; ``ValueError`` with a readable message."""
    devices = _merged(
        (_profile_for(row, n, profiles), row.quantity)
        for n, row in enumerate(model.rows, 1)
        if row.quantity > 0
    )
    if not devices:
        raise ValueError("Add at least one device (a profile with a quantity above 0)")
    return _request(
        model, devices, presets=presets, scan=scan, zones=zones, channel_plan=channel_plan
    )


def check_input(
    model: CoordinationModel,
    *,
    profiles: Mapping[str, DeviceProfile],
    presets: Mapping[str, SpacingRules],
    scan: Trace | None,
    zones: Sequence[ExclusionZone],
    channel_plan: ChannelPlan | None,
) -> tuple[CoordinationRequest, list[Assignment]]:
    """The request and the hand-made assignments typed in the rows' check boxes.

    Devices are labelled ``<profile> #n`` (numbered per profile over all rows), in row order and
    sorted by frequency within a row.
    """
    counters: dict[str, int] = {}
    assignments: list[Assignment] = []
    devices: list[tuple[DeviceProfile, int]] = []
    for n, row in enumerate(model.rows, 1):
        if not row.check_text.strip():
            continue
        profile = _profile_for(row, n, profiles)
        parsed = parse_channel_text(row.check_text)
        if parsed.errors:
            raise ValueError(f"{row.profile} (row {n}): {parsed.errors[0]}")
        for f in parsed.values_hz:
            counters[profile.name] = counters.get(profile.name, 0) + 1
            label = f"{profile.name} #{counters[profile.name]}"
            assignments.append(Assignment(label, profile.name, f))
        devices.append((profile, len(parsed.values_hz)))
    if not assignments:
        raise ValueError("Type or paste frequencies in the check boxes of the device rows first")
    request = _request(
        model,
        _merged(devices),
        presets=presets,
        scan=scan,
        zones=zones,
        channel_plan=channel_plan,
    )
    return request, assignments


# --- result (solved plan) ---------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinationResult:
    """A solved plan with what it was made around (for the panel, exports and sessions)."""

    plan: Plan
    locked: tuple[LockRow, ...]
    #: Label of the scan trace used, ``None`` when coordinated without one.
    scan_label: str | None
    #: When it was solved (ISO 8601, UTC).
    created: str
    #: Fingerprint of the setup it was solved from (``CoordinationActions.solve_key``).
    solve_key: str = ""
    #: Digest of the scan data used (``None``: no scan); a change only gives a note.
    scan_key: str | None = None


def _violation_to_dict(v: Violation | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "rule": v.rule,
        "required_hz": v.required_hz,
        "actual_hz": v.actual_hz,
        "sources": list(v.sources),
        "victim": v.victim,
        "product_hz": v.product_hz,
    }


def result_to_dict(result: CoordinationResult) -> dict[str, Any]:
    plan = result.plan
    s = plan.stats
    return {
        "created": result.created,
        "scan_label": result.scan_label,
        "solve_key": result.solve_key,
        "scan_key": result.scan_key,
        "assignments": [
            {
                "label": a.label,
                "profile": a.profile_name,
                "freq_hz": a.freq_hz,
                "group": a.group,
                "scan_level_dbm": a.scan_level_dbm,
                "imd_margin_hz": a.nearest_imd_margin_hz,
            }
            for a in plan.assignments
        ],
        "unassigned": [
            {
                "label": u.label,
                "profile": u.profile_name,
                "reason": u.reason,
                "blocked_by": _violation_to_dict(u.blocked_by),
            }
            for u in plan.unassigned
        ],
        "backups": {name: list(freqs) for name, freqs in plan.backups.items()},
        "warnings": list(plan.warnings),
        "stats": {
            "elapsed_s": s.elapsed_s,
            "nodes": s.nodes,
            "complete": s.complete,
            "timed_out": s.timed_out,
        },
        "locked": [
            {"freq_hz": lk.freq_hz, "label": lk.label, "preset": lk.preset} for lk in result.locked
        ],
    }


def _violation_from_dict(rec: Mapping[str, Any] | None) -> Violation | None:
    if rec is None:
        return None
    sources = _required(rec, "sources", list)
    if not all(isinstance(x, str) for x in sources):
        raise ValueError("'sources' must be a list of labels")
    rule = _required(rec, "rule", str)
    return Violation(
        rule,
        _required(rec, "required_hz", int),
        _required(rec, "actual_hz", int),
        tuple(sources),
        _typed(rec, "victim", str, None),
        _typed(rec, "product_hz", int, None),
    )


_REASONS: Final = frozenset(get_args(Reason))


def result_from_dict(data: Mapping[str, Any]) -> CoordinationResult:
    """A result from :func:`result_to_dict` output; ``ValueError`` on missing or bad fields."""
    assignments = tuple(
        Assignment(
            _required(rec, "label", str),
            _required(rec, "profile", str),
            _required(rec, "freq_hz", int),
            _typed(rec, "group", str, None),
            _optional_number(rec, "scan_level_dbm"),
            _typed(rec, "imd_margin_hz", int, None),
        )
        for rec in _records(data, "assignments", required=True)
    )
    unassigned = []
    for rec in _records(data, "unassigned"):
        reason = _required(rec, "reason", str)
        if reason not in _REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        unassigned.append(
            Unassigned(
                _required(rec, "label", str),
                _required(rec, "profile", str),
                cast(Reason, reason),
                _violation_from_dict(_typed(rec, "blocked_by", dict, None)),
            )
        )
    backups: dict[str, tuple[int, ...]] = {}
    for name, freqs in _typed(data, "backups", dict, {}).items():
        if not isinstance(freqs, list) or not all(_is_int(f) for f in freqs):
            raise ValueError(f"backups of {name!r} must be a list of Hz values")
        backups[str(name)] = tuple(freqs)
    warnings = _typed(data, "warnings", list, [])
    if not all(isinstance(w, str) for w in warnings):
        raise ValueError("'warnings' must be a list of texts")
    st = _required(data, "stats", dict)
    stats = SolveStats(
        float(_required(st, "elapsed_s", (int, float))),
        _required(st, "nodes", int),
        _required(st, "complete", bool),
        _required(st, "timed_out", bool),
    )
    locked = tuple(
        LockRow(
            _required(rec, "freq_hz", int),
            _required(rec, "label", str),
            _typed(rec, "preset", str, DEFAULT_LOCK_PRESET),
        )
        for rec in _records(data, "locked")
    )
    labels = [a.label for a in assignments] + [u.label for u in unassigned]
    if len(set(labels)) != len(labels):
        raise ValueError("device labels must be unique")
    for f in (
        *(a.freq_hz for a in assignments),
        *(lk.freq_hz for lk in locked),
        *(f for fs in backups.values() for f in fs),
    ):
        _check_freq(f)
    plan = Plan(
        assignments,
        tuple(unassigned),
        types.MappingProxyType(backups),
        tuple(warnings),
        stats,
    )
    return CoordinationResult(
        plan,
        locked,
        _typed(data, "scan_label", str, None),
        _typed(data, "created", str, ""),
        _typed(data, "solve_key", str, ""),
        _typed(data, "scan_key", str, None),
    )


# --- JSON field helpers -----------------------------------------------------------------------

#: Highest frequency accepted from a session (well above any RF Explorer).
MAX_FREQ_HZ: Final = 10_000_000_000


def _check_freq(freq_hz: int) -> None:
    if not 0 < freq_hz <= MAX_FREQ_HZ:
        raise ValueError(f"frequency {freq_hz} Hz is out of range")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _type_ok(value: object, kind: type | tuple[type, ...]) -> bool:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:
        return False
    return isinstance(value, kinds)


def _typed(rec: Mapping[str, Any], key: str, kind: type | tuple[type, ...], default: Any) -> Any:
    value = rec.get(key)
    if value is None:
        return default
    if not _type_ok(value, kind):
        raise ValueError(f"'{key}' has the wrong type")
    return value


def _required(rec: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    value = rec.get(key)
    if value is None or not _type_ok(value, kind):
        raise ValueError(f"'{key}' is missing or has the wrong type")
    return value


def _number(rec: Mapping[str, Any], key: str, default: float) -> float:
    return float(_typed(rec, key, (int, float), default))


def _optional_number(rec: Mapping[str, Any], key: str) -> float | None:
    value = _typed(rec, key, (int, float), None)
    return None if value is None else float(value)


def _records(
    rec: Mapping[str, Any], key: str, *, required: bool = False
) -> list[Mapping[str, Any]]:
    items = _required(rec, key, list) if required else _typed(rec, key, list, [])
    if not all(isinstance(i, dict) for i in items):
        raise ValueError(f"'{key}' must be a list of objects")
    return cast(list[Mapping[str, Any]], items)


__all__ = [
    "DEFAULT_LOCK_PRESET",
    "MAX_DEVICE_ROWS",
    "MAX_LOCKS",
    "CoordinationModel",
    "CoordinationResult",
    "DeviceRow",
    "LockRow",
    "Options",
    "build_request",
    "check_input",
    "lock_label",
    "result_from_dict",
    "result_to_dict",
    "run_override",
]
