"""Frequency coordination solver and plan checker (pure).

``solve(request) -> Plan``:

1. Candidate grid per profile entry (``profiles.candidates``).
2. Drop candidates in exclusion zones, in channel-plan ``forbidden`` bands/channels (unless
   ``allow_forbidden``) and occupied ones: the scan's maximum within ``± guard_hz`` (edges
   inclusive) is above ``noise_floor + threshold_db``.
3. Rank the rest: legal before allowed-forbidden, known scan level before unknown, quietest
   first, then lowest frequency.
4. Depth-first branch and bound over the device instances with an incremental
   ``ProductSet``: the profile with the fewest usable candidates goes next (dynamic
   most-constrained-first, forward-checked domains via ``conflict_mask``); instances of one
   profile are interchangeable, so they take candidates in increasing rank (no symmetric
   branches). Each node may also *skip* a device, so the search maximises the assigned count;
   it stops at the first complete plan or at the time budget (best partial: most devices,
   then fewest unknown scan levels, then lowest total scan level).
5. With ``prefer_single_group`` a first pass keeps every device of a grouped profile in one
   group (groups tried in order of usable capacity); if it does not place everything a second
   pass allows mixing (the first pass gets half the budget).

``check(assignments, request)`` validates any plan with the same rules, independently of the
search (it enumerates the products of all carriers), and lists every violation.

Locked carriers and profiles use the request's run override as well, so a "tight mode" scale
applies to every carrier.
"""

from __future__ import annotations

import bisect
import functools
import itertools
import time
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from opencoord.coord.channel_plans.model import ChannelPlan
from opencoord.coord.imd import KINDS, CarrierId, Conflict, ProductSet
from opencoord.coord.profiles import DeviceProfile, candidates
from opencoord.coord.spacing import (
    PartialSpacing,
    RunOverride,
    SpacingRules,
    builtin_presets,
    resolve_profile_rules,
)
from opencoord.core.traces import noise_floor
from opencoord.core.types import ExclusionZone, Trace

I64 = npt.NDArray[np.int64]
F64 = npt.NDArray[np.float64]
Bools = npt.NDArray[np.bool_]

Reason = Literal[
    "no-candidates-in-range",
    "all-candidates-excluded",
    "all-candidates-occupied",
    "imd-conflicts",
    "time-budget",
]
_RULE_ORDER: tuple[str, ...] = ("carrier", *KINDS)
#: Most devices one request may ask for (keeps the depth-first search well inside Python's
#: recursion limit).
MAX_DEVICES = 200


@functools.cache
def _builtin_rules() -> dict[str, SpacingRules]:
    return {name: p.rules for name, p in builtin_presets().items()}


def _default_locked_rules() -> SpacingRules:
    return _builtin_rules()["generic-analog"]


def _default_presets() -> dict[str, SpacingRules]:
    return dict(_builtin_rules())


# ---------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class LockedCarrier:
    """A fixed transmitter (foreign, or a device already tuned) that takes part in the checks."""

    freq_hz: int
    label: str
    rules: SpacingRules = field(default_factory=_default_locked_rules)


@dataclass(frozen=True)
class CoordinationRequest:
    """Everything one coordination run needs; ``presets`` maps preset name to its rules."""

    devices: Sequence[tuple[DeviceProfile, int]]
    locked: Sequence[LockedCarrier] = ()
    scan: Trace | None = None
    zones: Sequence[ExclusionZone] = ()
    channel_plan: ChannelPlan | None = None
    allow_forbidden: bool = False
    threshold_db: float = 10.0
    guard_hz: int = 100_000
    prefer_single_group: bool = True
    time_budget_s: float = 5.0
    run_override: RunOverride | None = None
    backups_per_profile: int = 2
    presets: Mapping[str, SpacingRules] = field(default_factory=_default_presets)
    clock: Callable[[], float] = field(default=time.monotonic, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "devices", tuple((p, q) for p, q in self.devices))
        object.__setattr__(self, "locked", tuple(self.locked))
        object.__setattr__(self, "zones", tuple(self.zones))
        names: set[str] = set()
        for profile, quantity in self.devices:
            if quantity < 0:
                raise ValueError(f"{profile.name}: quantity must be 0 or more, got {quantity}")
            if profile.name in names:
                raise ValueError(
                    f"profile {profile.name!r} is listed twice; combine the quantities instead"
                )
            names.add(profile.name)
        total = sum(q for _, q in self.devices)
        if total > MAX_DEVICES:
            raise ValueError(f"at most {MAX_DEVICES} devices per coordination, got {total}")
        if self.threshold_db < 0:
            raise ValueError(f"threshold must be 0 dB or more, got {self.threshold_db}")
        if self.guard_hz < 0:
            raise ValueError(f"guard bandwidth must be 0 Hz or more, got {self.guard_hz}")
        if not self.time_budget_s > 0:
            raise ValueError(f"time budget must be above 0 s, got {self.time_budget_s}")
        if self.backups_per_profile < 0:
            raise ValueError(
                f"backups per profile must be 0 or more, got {self.backups_per_profile}"
            )


@dataclass(frozen=True)
class Violation:
    """A broken rule. ``sources``: the carriers involved (labels; for a product the ones that
    form it). ``victim``: the carrier a product lands on (``None`` for carrier spacing, or when
    explaining a candidate that is itself hit). ``product_hz``: ``None`` for carrier spacing."""

    rule: str
    required_hz: int
    actual_hz: int
    sources: tuple[str, ...]
    victim: str | None
    product_hz: int | None


@dataclass(frozen=True)
class Assignment:
    """A device on a frequency. ``nearest_imd_margin_hz`` is the distance from ``freq_hz`` to the
    nearest IMD product of the other carriers in the plan (``None`` when there is none); a
    hand-made plan for ``check`` only needs ``label``, ``profile_name`` and ``freq_hz``."""

    label: str
    profile_name: str
    freq_hz: int
    group: str | None = None
    scan_level_dbm: float | None = None
    nearest_imd_margin_hz: int | None = None


@dataclass(frozen=True)
class Unassigned:
    """A device without a frequency; ``blocked_by`` explains why its best-ranked unused
    candidate is not usable in the final plan (``imd-conflicts`` / ``time-budget`` only)."""

    label: str
    profile_name: str
    reason: Reason
    blocked_by: Violation | None = None


@dataclass(frozen=True)
class SolveStats:
    elapsed_s: float
    nodes: int
    complete: bool  # every device has a frequency
    timed_out: bool


@dataclass(frozen=True)
class Plan:
    """A coordination result. ``backups`` (read-only, profile name -> frequencies, best first)
    are chosen greedily round-robin over the profiles, each one checked against the plan, the
    locked carriers **and every backup chosen before it**: the plan plus all backups together
    pass ``check``, so any of them can be brought in at the same time."""

    assignments: tuple[Assignment, ...]
    unassigned: tuple[Unassigned, ...]
    backups: Mapping[str, tuple[int, ...]]  # profile name -> spare frequencies (best first)
    warnings: tuple[str, ...]
    stats: SolveStats


@dataclass(frozen=True)
class CheckReport:
    violations: tuple[Violation, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------- helpers


def _mhz(hz: float) -> str:
    return f"{hz / 1e6:.3f} MHz"


def _khz(hz: int) -> str:
    return f"{hz / 1e3:g} kHz"


def _profile_rules(profile: DeviceProfile, request: CoordinationRequest) -> SpacingRules:
    preset = request.presets.get(profile.spacing_preset)
    if preset is None:
        available = ", ".join(sorted(request.presets)) or "(none)"
        raise ValueError(
            f"{profile.name}: unknown spacing preset {profile.spacing_preset!r} "
            f"(available: {available})"
        )
    return resolve_profile_rules(preset, profile.spacing_overrides, request.run_override)


def _locked_rules(locked: LockedCarrier, request: CoordinationRequest) -> SpacingRules:
    return resolve_profile_rules(locked.rules, PartialSpacing(), request.run_override)


def _zone_mask(zones: Sequence[ExclusionZone], f: I64) -> Bools:
    out = np.zeros(f.shape, dtype=bool)
    for z in zones:
        out |= (f >= z.start_hz) & (f <= z.stop_hz)
    return out


def _forbidden_spans(plan: ChannelPlan | None) -> list[tuple[int, int, str]]:
    """``[start, stop)`` spans the plan marks forbidden, with a note."""
    if plan is None:
        return []
    spans = [(b.start_hz, b.stop_hz, b.note) for b in plan.bands if b.pmse == "forbidden"]
    spans += [
        (c.start_hz, c.stop_hz, f"channel {c.number}")
        for c in plan.channels
        if c.pmse == "forbidden"
    ]
    return spans


def _forbidden_mask(plan: ChannelPlan | None, f: I64) -> Bools:
    out = np.zeros(f.shape, dtype=bool)
    for lo, hi, _ in _forbidden_spans(plan):
        out |= (f >= lo) & (f < hi)
    return out


def _forbidden_note(plan: ChannelPlan | None, freq_hz: int) -> str | None:
    for lo, hi, note in _forbidden_spans(plan):
        if lo <= freq_hz < hi:
            return note
    return None


def _scan_levels(scan: Trace | None, f: I64, guard_hz: int) -> F64:
    """Maximum scan level within ``f ± guard`` (edges inclusive); ``nan`` = unknown.

    A candidate inside the scan span whose window holds no bin uses its two neighbouring bins.
    """
    out = np.full(f.shape, np.nan)
    if scan is None or scan.freqs_hz.size == 0 or f.size == 0:
        return out
    order = np.argsort(scan.freqs_hz, kind="stable")
    tf = scan.freqs_hz[order]
    lv = scan.dbm[order].astype(np.float64)
    x = f.astype(np.float64)
    lo = np.searchsorted(tf, x - guard_hz, side="left")
    hi = np.searchsorted(tf, x + guard_hz, side="right")
    empty = hi <= lo
    inside = (x >= tf[0]) & (x <= tf[-1])
    fix = empty & inside
    nxt = np.searchsorted(tf, x[fix], side="left")
    lo[fix] = np.maximum(nxt - 1, 0)
    hi[fix] = np.minimum(nxt + 1, tf.size)
    known = hi > lo
    # sparse table range maximum
    table = [lv]
    while (1 << len(table)) <= tf.size:
        prev, half = table[-1], 1 << (len(table) - 1)
        table.append(np.maximum(prev[:-half], prev[half:]))
    length = (hi - lo)[known]
    k = np.floor(np.log2(np.maximum(length, 1))).astype(np.int64)
    a, b = lo[known], hi[known] - (1 << k)
    res = np.empty(length.shape)
    for level in np.unique(k).tolist():
        sel = k == level
        res[sel] = np.maximum(table[level][a[sel]], table[level][b[sel]])
    out[known] = res
    return out


def _occupied(scan: Trace | None, levels: F64, threshold_db: float) -> Bools:
    if scan is None or scan.dbm.size == 0:
        return np.zeros(levels.shape, dtype=bool)
    limit = noise_floor(scan.dbm) + threshold_db
    return np.nan_to_num(levels, nan=-np.inf) > limit


def _merge(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _profile_ranges(profile: DeviceProfile) -> list[tuple[int, int]]:
    """Ranges covering the profile's candidates, from the same source ``candidates()`` uses."""
    if profile.groups:
        chans = [c for g in profile.groups for c in g.channels]
    elif profile.channels:
        chans = list(profile.channels)
    elif profile.step_hz:
        return [(r.start_hz, r.stop_hz) for r in profile.tuning]
    else:
        chans = []
    return [(min(chans), max(chans))] if chans else []


# ---------------------------------------------------------------------------- violations


@dataclass(frozen=True)
class _Tx:
    cid: CarrierId
    label: str
    freq: int
    rules: SpacingRules


def _violations(txs: Sequence[_Tx]) -> list[tuple[Violation, frozenset[CarrierId]]]:
    """Every rule broken among ``txs`` with the ids involved, independent of the search:
    carrier pairs directly, products from the full product set of all carriers."""
    out: list[tuple[Violation, frozenset[CarrierId]]] = []
    for a, b in itertools.combinations(txs, 2):
        need = max(a.rules.carrier, b.rules.carrier)
        dist = abs(a.freq - b.freq)
        if dist < need:
            v = Violation("carrier", need, dist, (a.label, b.label), None, None)
            out.append((v, frozenset((a.cid, b.cid))))
    margin = max((getattr(t.rules, k) for t in txs for k in KINDS), default=0)
    if margin == 0 or len(txs) < 2:
        return out
    pset = ProductSet(_merge([(t.freq - 1, t.freq + 1) for t in txs]), [t.rules for t in txs])
    for t in txs:
        pset.add(t.freq, t.rules, t.cid)
    by_id = {t.cid: t for t in txs}
    ordered = sorted(txs, key=lambda t: t.freq)
    freqs = [t.freq for t in ordered]
    for prod in pset.products():
        lo = bisect.bisect_right(freqs, prod.freq_hz - margin)
        hi = bisect.bisect_left(freqs, prod.freq_hz + margin)
        if lo >= hi:
            continue
        sources = prod.sources
        from_sources = max(getattr(by_id[s].rules, prod.kind) for s in sources)
        for victim in ordered[lo:hi]:
            if victim.cid in sources:
                continue
            need = max(from_sources, getattr(victim.rules, prod.kind))
            dist = abs(prod.freq_hz - victim.freq)
            if dist < need:
                labels = tuple(by_id[s].label for s in sources)
                v = Violation(prod.kind, need, dist, labels, victim.label, prod.freq_hz)
                out.append((v, frozenset((*sources, victim.cid))))
    out.sort(
        key=lambda item: (
            item[0].product_hz if item[0].product_hz is not None else 0,
            _RULE_ORDER.index(item[0].rule),
            item[0].sources,
            item[0].victim or "",
        )
    )
    return out


#: Short names of the rules for messages.
RULE_TEXT: Mapping[str, str] = types.MappingProxyType(
    {
        "carrier": "carrier spacing",
        "im3_2tx": "3rd order 2-Tx",
        "im3_3tx": "3rd order 3-Tx",
        "im5_2tx": "5th order 2-Tx",
        "im7_2tx": "7th order 2-Tx",
        "im5_3tx": "5th order 3-Tx",
    }
)
#: Why a device got no frequency, in words.
REASON_TEXT: Mapping[Reason, str] = types.MappingProxyType(
    {
        "no-candidates-in-range": "the profile has no frequencies to choose from",
        "all-candidates-excluded": (
            "every frequency of the profile is in an exclusion zone or a forbidden band"
        ),
        "all-candidates-occupied": "every frequency of the profile is occupied in the scan",
        "imd-conflicts": (
            "no frequency left that is clear of the other carriers and their intermods"
        ),
        "time-budget": "the time budget ran out before a frequency was found",
    }
)


def describe_violation(v: Violation) -> str:
    """One line explaining a broken rule (labels, MHz and kHz)."""
    if v.product_hz is None:
        who = " and ".join(v.sources)
        return f"carrier spacing: {who} are {_khz(v.actual_hz)} apart (needs {_khz(v.required_hz)})"
    victim = v.victim if v.victim is not None else "this frequency"
    return (
        f"{RULE_TEXT.get(v.rule, v.rule)}: product of {', '.join(v.sources)} at "
        f"{_mhz(v.product_hz)} is {_khz(v.actual_hz)} from {victim} (needs {_khz(v.required_hz)})"
    )


def describe_unassigned(u: Unassigned) -> str:
    """Why ``u`` has no frequency, plus what blocks its best spot when known."""
    text = REASON_TEXT[u.reason]
    if u.blocked_by is not None:
        text += f"; best spot blocked by {describe_violation(u.blocked_by)}"
    return text


def _locked_txs(request: CoordinationRequest) -> list[_Tx]:
    return [
        _Tx(("locked", i), lc.label, lc.freq_hz, _locked_rules(lc, request))
        for i, lc in enumerate(request.locked)
    ]


def _locked_clash_warnings(locked: Sequence[_Tx]) -> list[str]:
    return [f"Locked carriers clash: {describe_violation(v)}" for v, _ in _violations(locked)]


# ---------------------------------------------------------------------------- preparation


@dataclass
class _Entry:
    """One ``(profile, quantity)`` of the request with its ranked, usable candidates."""

    profile: DeviceProfile
    quantity: int
    rules: SpacingRules
    labels: tuple[str, ...]
    freqs: I64 = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    levels: F64 = field(default_factory=lambda: np.empty(0))
    groups: tuple[tuple[str, ...], ...] = ()
    members: dict[str, Bools] = field(default_factory=dict)
    reason: Reason | None = None  # set when nothing survives filtering
    start_empty: bool = False  # every candidate already conflicts with the locked carriers


def _prepare(request: CoordinationRequest) -> list[_Entry]:
    counters: dict[str, int] = {}
    entries: list[_Entry] = []
    for profile, quantity in request.devices:
        rules = _profile_rules(profile, request)
        start = counters.get(profile.name, 0)
        counters[profile.name] = start + quantity
        labels = tuple(f"{profile.name} #{start + i + 1}" for i in range(quantity))
        entry = _Entry(profile, quantity, rules, labels)
        entries.append(entry)
        if quantity == 0:
            continue
        cands = candidates(profile)
        if not cands:
            entry.reason = "no-candidates-in-range"
            continue
        f = np.array([c.freq_hz for c in cands], dtype=np.int64)
        forbidden = _forbidden_mask(request.channel_plan, f)
        excluded = _zone_mask(request.zones, f)
        if not request.allow_forbidden:
            excluded |= forbidden
        if excluded.all():
            entry.reason = "all-candidates-excluded"
            continue
        levels = _scan_levels(request.scan, f, request.guard_hz)
        keep = ~excluded & ~_occupied(request.scan, levels, request.threshold_db)
        if not keep.any():
            entry.reason = "all-candidates-occupied"
            continue
        idx = np.flatnonzero(keep)
        unknown = np.isnan(levels[idx])
        order = np.lexsort((f[idx], np.nan_to_num(levels[idx]), unknown, forbidden[idx]))
        idx = idx[order]
        entry.freqs = f[idx]
        entry.levels = levels[idx]
        entry.groups = tuple(cands[i].groups for i in idx.tolist())
        entry.members = {
            g.name: np.array([g.name in gs for gs in entry.groups], dtype=bool)
            for g in profile.groups
        }
    return entries


# ---------------------------------------------------------------------------- search


class _Timeout(Exception):
    pass


Placement = tuple[int, str | None]  # (rank position, group)


class _Search:
    """One branch-and-bound pass (see the module docstring)."""

    def __init__(
        self,
        entries: Sequence[_Entry],
        pset: ProductSet,
        single_group: bool,
        clock: Callable[[], float],
        deadline: float,
    ) -> None:
        self.entries = entries
        self.pset = pset
        self.single_group = single_group
        self.clock = clock
        self.deadline = deadline
        self.active = [i for i, e in enumerate(entries) if e.reason is None and e.quantity]
        self.total = sum(entries[i].quantity for i in self.active)
        self.remaining = [e.quantity if i in self.active else 0 for i, e in enumerate(entries)]
        self.placed: list[list[Placement]] = [[] for _ in entries]
        self.chosen: list[str | None] = [None for _ in entries]
        self.count = 0
        self.unknown = 0
        self.level_sum = 0.0
        self.nodes = 0
        self.best_key: tuple[int, int, float] = (-1, 0, 0.0)
        self.best: list[list[Placement]] = [[] for _ in entries]
        self.timed_out = False

    def run(self) -> bool:
        domains: list[I64] = []
        for i, e in enumerate(self.entries):
            if i in self.active:
                pos = np.arange(e.freqs.size, dtype=np.int64)
                domains.append(pos[~self.pset.conflict_mask(e.freqs, e.rules)])
            else:
                domains.append(np.empty(0, dtype=np.int64))
        try:
            return self._node(domains)
        except (_Timeout, RecursionError):  # MAX_DEVICES keeps the depth well below the limit
            self.timed_out = True
            return False

    def _usable(self, i: int, dom: I64) -> I64:
        placed = self.placed[i]
        if placed:
            dom = dom[dom > placed[-1][0]]
        g = self.chosen[i]
        if g is not None:
            dom = dom[self.entries[i].members[g][dom]]
        return dom

    def _group_order(self, i: int, dom: I64) -> list[tuple[str, int]]:
        e = self.entries[i]
        caps = [(g.name, int(e.members[g.name][dom].sum())) for g in e.profile.groups]
        return sorted(caps, key=lambda gc: -gc[1])  # stable: definition order on ties

    def _picks_group(self, i: int) -> bool:
        return self.single_group and bool(self.entries[i].members) and self.chosen[i] is None

    def _node(self, domains: list[I64]) -> bool:
        self.nodes += 1
        key = (self.count, -self.unknown, -self.level_sum)
        if key > self.best_key:
            self.best_key = key
            self.best = [list(p) for p in self.placed]
        todo = sum(self.remaining)
        if todo == 0:
            return self.count == self.total
        if self.clock() >= self.deadline:
            raise _Timeout
        if self.count + todo <= self.best_key[0]:
            return False

        pick, usable, size = -1, np.empty(0, dtype=np.int64), -1
        for i in self.active:
            if not self.remaining[i]:
                continue
            dom = self._usable(i, domains[i])
            n = (
                max((c for _, c in self._group_order(i, dom)), default=0)
                if self._picks_group(i)
                else int(dom.size)
            )
            if size < 0 or n < size:
                pick, usable, size = i, dom, n
        if size == 0:
            return self._skip(pick, self.remaining[pick], domains)

        for pos, group in self._branches(pick, usable):
            if self._place(pick, pos, group, domains):
                return True
        return self._skip(pick, 1, domains)

    def _branches(self, i: int, usable: I64) -> Iterator[Placement]:
        e = self.entries[i]
        if self._picks_group(i):
            for g, cap in self._group_order(i, usable):
                if cap:
                    for pos in usable[e.members[g][usable]].tolist():
                        yield pos, g
            return
        for pos in usable.tolist():
            yield pos, self.chosen[i] or (e.groups[pos][0] if e.groups[pos] else None)

    def _place(self, i: int, pos: int, group: str | None, domains: list[I64]) -> bool:
        e = self.entries[i]
        cid = ("device", i, len(self.placed[i]))
        picks_group = self._picks_group(i)
        self.pset.add(int(e.freqs[pos]), e.rules, cid)
        self.placed[i].append((pos, group))
        if picks_group:
            self.chosen[i] = group
        self.remaining[i] -= 1
        self.count += 1
        level = float(e.levels[pos])
        known = not np.isnan(level)
        if known:
            self.level_sum += level
        else:
            self.unknown += 1
        try:
            child = list(domains)
            for q in self.active:
                d = domains[q]
                if self.remaining[q] and d.size:
                    eq = self.entries[q]
                    child[q] = d[~self.pset.conflict_mask(eq.freqs[d], eq.rules)]
            return self._node(child)
        finally:
            if known:
                self.level_sum -= level
            else:
                self.unknown -= 1
            self.count -= 1
            self.remaining[i] += 1
            if picks_group:
                self.chosen[i] = None
            self.placed[i].pop()
            self.pset.remove(cid)

    def _skip(self, i: int, k: int, domains: list[I64]) -> bool:
        self.remaining[i] -= k
        try:
            return self._node(domains)
        finally:
            self.remaining[i] += k


# ---------------------------------------------------------------------------- solve


def _product_set(entries: Sequence[_Entry], locked: Sequence[_Tx]) -> ProductSet | None:
    ranges = [r for e in entries for r in _profile_ranges(e.profile)]
    ranges += [(t.freq - 1, t.freq + 1) for t in locked]
    if not ranges:
        return None
    rules = [e.rules for e in entries] + [t.rules for t in locked]
    return ProductSet(_merge(ranges), rules)


def solve(request: CoordinationRequest) -> Plan:
    """Coordinate ``request``; always returns a plan (complete, or the best partial one)."""
    clock = request.clock
    started = clock()
    entries = _prepare(request)
    locked = _locked_txs(request)
    warnings = _locked_clash_warnings(locked)

    best: list[list[Placement]] = [[] for _ in entries]
    nodes, timed_out, last_timed_out = 0, False, False
    pset = _product_set(entries, locked)
    if pset is not None:
        for t in locked:
            pset.add(t.freq, t.rules, t.cid)
        for e in entries:
            if e.reason is None and e.quantity:
                e.start_empty = bool(pset.conflict_mask(e.freqs, e.rules).all())
        deadline = started + request.time_budget_s
        grouped = request.prefer_single_group and any(e.members for e in entries)
        passes = [True, False] if grouped else [False]
        best_key: tuple[int, int, float] | None = None
        for n, single_group in enumerate(passes):
            last = n == len(passes) - 1
            limit = deadline if last else started + request.time_budget_s / 2
            search = _Search(entries, pset, single_group, clock, limit)
            done = search.run()  # every placement is undone on return, timeout included
            nodes += search.nodes
            if best_key is None or search.best_key > best_key:
                best_key, best = search.best_key, search.best
            timed_out = timed_out or search.timed_out
            last_timed_out = search.timed_out
            if done:
                break

    return _assemble(
        request, entries, locked, best, warnings, nodes, timed_out, last_timed_out, started
    )


def _assemble(
    request: CoordinationRequest,
    entries: Sequence[_Entry],
    locked: Sequence[_Tx],
    best: Sequence[Sequence[Placement]],
    warnings: list[str],
    nodes: int,
    timed_out: bool,
    cut_off: bool,
    started: float,
) -> Plan:
    """``timed_out``: any pass hit the deadline; ``cut_off``: the last (widest) pass did, so
    its result is not proven optimal and unplaced devices get the ``time-budget`` reason."""
    txs = list(locked)
    rows: list[tuple[str, _Entry, int, str | None, float | None]] = []
    for i, e in enumerate(entries):
        for k, (pos, group) in enumerate(sorted(best[i], key=lambda p: int(e.freqs[p[0]]))):
            raw = float(e.levels[pos])
            rows.append((e.labels[k], e, pos, group, None if np.isnan(raw) else raw))
            txs.append(_Tx(("device", i, k), e.labels[k], int(e.freqs[pos]), e.rules))
    final = _product_set(entries, locked)
    if final is not None:
        for t in txs:
            final.add(t.freq, t.rules, t.cid)
    nearest = _nearest_products(final, txs)

    assignments: list[Assignment] = []
    for (label, e, pos, group, level), t in zip(rows, txs[len(locked) :], strict=True):
        f = int(e.freqs[pos])
        assignments.append(Assignment(label, e.profile.name, f, group, level, nearest.get(t.cid)))
        note = _forbidden_note(request.channel_plan, f)
        if note is not None:
            warnings.append(f"{label} at {_mhz(f)} is in a forbidden band: {note}")

    names = {t.cid: t.label for t in txs}
    unassigned: list[Unassigned] = []
    used = {t.freq for t in txs}
    for i, e in enumerate(entries):
        if not e.quantity:
            continue
        if e.reason is not None:
            unassigned += [Unassigned(lb, e.profile.name, e.reason) for lb in e.labels]
            continue
        n_placed = len(best[i])
        if n_placed == e.quantity:
            continue
        reason: Reason = "time-budget" if cut_off and not e.start_empty else "imd-conflicts"
        explain = None if final is None else _explain(final, e, used, names)
        unassigned += [
            Unassigned(lb, e.profile.name, reason, explain) for lb in e.labels[n_placed:]
        ]
    backups = _backups(request, entries, best, final, used)

    total = sum(e.quantity for e in entries)
    stats = SolveStats(
        elapsed_s=request.clock() - started,
        nodes=nodes,
        complete=len(assignments) == total,
        timed_out=timed_out,
    )
    return Plan(
        tuple(assignments),
        tuple(unassigned),
        types.MappingProxyType(backups),
        tuple(warnings),
        stats,
    )


def _backups(
    request: CoordinationRequest,
    entries: Sequence[_Entry],
    best: Sequence[Sequence[Placement]],
    final: ProductSet | None,
    used: set[int],
) -> dict[str, tuple[int, ...]]:
    """Spare frequencies per profile, mutually compatible with each other and the plan.

    Round-robin over the profiles, each taking its best-ranked candidate (its single group
    first) that is clean against the plan and every backup chosen so far; each pick is added
    to ``final`` before the next one is masked.
    """
    ranked: list[tuple[_Entry, I64]] = []
    for i, e in enumerate(entries):
        if e.reason is not None or not e.quantity:
            continue
        order = np.arange(e.freqs.size, dtype=np.int64)
        groups = {g for _, g in best[i]}
        if e.members and len(groups) == 1 and None not in groups:
            (g,) = groups
            order = order[np.argsort(~e.members[str(g)], kind="stable")]
        ranked.append((e, order))
    picked: dict[str, list[int]] = {e.profile.name: [] for e, _ in ranked}
    if final is None:
        return dict.fromkeys(picked, ())
    taken = set(used)
    for r in range(request.backups_per_profile):
        for e, order in ranked:
            free = order[~np.isin(e.freqs[order], list(taken))]
            clean = free[~final.conflict_mask(e.freqs[free], e.rules)]
            if clean.size:
                f = int(e.freqs[clean[0]])
                final.add(f, e.rules, ("backup", e.profile.name, r))
                taken.add(f)
                picked[e.profile.name].append(f)
    return {name: tuple(fs) for name, fs in picked.items()}


def _explain(
    pset: ProductSet, e: _Entry, used: set[int], names: Mapping[CarrierId, str]
) -> Violation | None:
    """Why the best-ranked unused candidate of ``e`` is blocked in the final plan."""
    for f in e.freqs.tolist():
        if f in used:
            continue
        c = pset.nearest_conflict(f, e.rules)
        return None if c is None else _to_violation(c, names)
    return None


def _to_violation(c: Conflict, names: Mapping[CarrierId, str]) -> Violation:
    victim = None if c.victim is None else names[c.victim]
    return Violation(
        c.rule, c.required_hz, c.actual_hz, tuple(names[s] for s in c.sources), victim, c.product_hz
    )


def _nearest_products(pset: ProductSet | None, txs: Sequence[_Tx]) -> dict[CarrierId, int]:
    """Distance from each carrier to the nearest stored product it is not a source of."""
    if pset is None:
        return {}
    products = pset.products()
    if not products or not txs:
        return {}
    column = {t.cid: k for k, t in enumerate(txs)}
    rows = [n for n, p in enumerate(products) for _ in p.terms]
    cols = [column[cid] for p in products for cid in p.sources]
    own = np.zeros((len(products), len(txs)), dtype=bool)
    own[rows, cols] = True
    freqs = np.array([p.freq_hz for p in products], dtype=np.int64)
    carriers = np.array([t.freq for t in txs], dtype=np.int64)
    dist = np.abs(freqs[:, None] - carriers[None, :])
    dist[own] = np.iinfo(np.int64).max
    nearest = dist.min(axis=0)
    return {t.cid: int(nearest[k]) for k, t in enumerate(txs) if not own[:, k].all()}


# ---------------------------------------------------------------------------- check


def check(assignments: Sequence[Assignment], request: CoordinationRequest) -> CheckReport:
    """Validate a (hand-made) plan against ``request``'s rules, locked carriers and scan.

    Every violation involving at least one assigned device is listed; clashes among locked
    carriers only, and frequencies in forbidden bands, exclusion zones, occupied spots or off
    the profile's candidates, are warnings.
    """
    rules: dict[str, SpacingRules] = {}
    grids: dict[str, set[int]] = {}
    for profile, _ in request.devices:
        if profile.name not in rules:
            rules[profile.name] = _profile_rules(profile, request)
            grids[profile.name] = {c.freq_hz for c in candidates(profile)}
    locked = _locked_txs(request)
    txs = list(locked)
    seen: set[str] = set()
    for a in assignments:
        if a.profile_name not in rules:
            raise ValueError(f"{a.label}: unknown profile {a.profile_name!r}")
        if a.label in seen:
            raise ValueError(f"device label {a.label!r} is used twice")
        seen.add(a.label)
        txs.append(_Tx(("device", a.label), a.label, a.freq_hz, rules[a.profile_name]))

    violations: list[Violation] = []
    warnings: list[str] = []
    for v, ids in _violations(txs):
        if any(isinstance(cid, tuple) and cid[0] == "device" for cid in ids):
            violations.append(v)
        else:
            warnings.append(f"Locked carriers clash: {describe_violation(v)}")

    if assignments:
        f = np.array([a.freq_hz for a in assignments], dtype=np.int64)
        levels = _scan_levels(request.scan, f, request.guard_hz)
        occupied = _occupied(request.scan, levels, request.threshold_db)
        in_zone = _zone_mask(request.zones, f)
        for k, a in enumerate(assignments):
            where = f"{a.label} at {_mhz(a.freq_hz)}"
            note = _forbidden_note(request.channel_plan, a.freq_hz)
            if note is not None:
                warnings.append(f"{where} is in a forbidden band: {note}")
            if in_zone[k]:
                warnings.append(f"{where} is inside an exclusion zone")
            if occupied[k]:
                warnings.append(f"{where} is occupied in the scan ({levels[k]:.1f} dBm)")
            if a.freq_hz not in grids[a.profile_name]:
                warnings.append(f"{where} is not a candidate of profile {a.profile_name!r}")
    return CheckReport(tuple(violations), tuple(warnings))


__all__ = [
    "MAX_DEVICES",
    "REASON_TEXT",
    "RULE_TEXT",
    "Assignment",
    "CheckReport",
    "CoordinationRequest",
    "LockedCarrier",
    "Plan",
    "Reason",
    "SolveStats",
    "Unassigned",
    "Violation",
    "check",
    "describe_unassigned",
    "describe_violation",
    "solve",
]
