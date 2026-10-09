"""Intermodulation engine: products, an incremental product set and conflict checks (pure).

All arithmetic is exact ``int64`` Hz. Product kinds are the ``SpacingRules`` IMD fields:

======== ============================= =========================================
kind     products (distinct carriers)  notes
======== ============================= =========================================
im3_2tx  ``2a - b``                    ordered pairs
im3_3tx  ``a + b - c``                 ``{a, b}`` unordered, ``c`` the third one
im5_2tx  ``3a - 2b``                   ordered pairs
im7_2tx  ``4a - 3b``                   ordered pairs
im5_3tx  ``2a + b - 2c``, ``3a - b - c`` every 5th-order 3-carrier form whose
                                       coefficients sum to 1 (lands near the
                                       carriers): 6 orderings + 3 per triple
======== ============================= =========================================

Conflict rules (documented choice):

* A product and the carrier it lands on ("victim") conflict when ``|product - victim| <
  required``, where ``required`` is the **stricter** (max) value of that kind over the
  victim's rules **and the rules of every source carrier**. A carrier is never the victim of
  a product it is a source of. ``0`` everywhere disables the kind.
* Carrier to carrier: ``|a - b| < resolve(a, b).carrier``.

``ProductSet`` keeps, for the placed carriers, every *check form* a candidate ``x`` could
violate: a sorted ``int64`` array of targets ``T`` per (form, required spacing), with the
condition ``|m·x - T| < max(required, candidate rule)``. ``m = 1`` forms are the products
themselves (x is hit); ``m > 1`` forms are the places where a product that *x creates* would
land on a placed carrier (x is the aggressor), e.g. ``2x - b = v`` becomes ``|2x - (b + v)|``.
Aggressor roles with coefficient ±1 are identical to an ``m = 1`` form (``2a - x = v`` is
``|x - (2a - v)|``), so they share it. Because ``required`` covers every carrier involved,
the two readings give the same answer. Targets are only kept inside the union of the tuning
ranges (scaled by ``m``) plus the largest IMD spacing, so candidates must lie in those ranges.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from opencoord.coord.spacing import SpacingRules

CarrierId = Hashable
I64 = npt.NDArray[np.int64]

KINDS: tuple[str, ...] = ("im3_2tx", "im3_3tx", "im5_2tx", "im7_2tx", "im5_3tx")
_RULE_ORDER: tuple[str, ...] = ("carrier", *KINDS)  # tie-break between equally bad conflicts


@dataclass(frozen=True)
class Product:
    """One intermodulation product: ``freq_hz = sum(coef * carrier_hz for coef, id in terms)``."""

    kind: str
    freq_hz: int
    terms: tuple[tuple[int, CarrierId], ...]

    @property
    def sources(self) -> tuple[CarrierId, ...]:
        return tuple(cid for _, cid in self.terms)

    @property
    def order(self) -> int:
        return sum(abs(c) for c, _ in self.terms)


@dataclass(frozen=True)
class Conflict:
    """Why a candidate is blocked.

    ``rule`` is a ``SpacingRules`` field name. ``sources`` are the placed carriers involved
    (for an aggressor conflict this includes the ``victim``). ``victim`` is ``None`` when the
    candidate itself is hit (or for a carrier-spacing conflict), otherwise the id of the placed
    carrier that a product created by the candidate lands on. ``product_hz`` is ``None`` for
    carrier spacing.
    """

    rule: str
    required_hz: int
    actual_hz: int
    sources: tuple[CarrierId, ...]
    victim: CarrierId | None
    product_hz: int | None


@dataclass(frozen=True)
class _Form:
    kind: str
    m: int  # multiplier of the candidate
    coefs: tuple[int, ...]  # T = sum(coefs * placed carriers)
    victim: int | None  # position of the placed carrier hit, None = candidate is hit (T = product)
    sign: int  # product = victim + sign * (m*x - T)


_FORMS: tuple[_Form, ...] = (
    _Form("im3_2tx", 1, (2, -1), None, 0),  # 2a-b hits x; also x as b: 2a-x = v
    _Form("im3_2tx", 2, (1, 1), 0, 1),  # x as a: 2x - b = v
    _Form("im3_3tx", 1, (1, 1, -1), None, 0),  # every role of x reduces to this form
    _Form("im5_2tx", 1, (3, -2), None, 0),
    _Form("im5_2tx", 3, (2, 1), 1, 1),  # 3x - 2b = v
    _Form("im5_2tx", 2, (3, -1), 1, -1),  # 3a - 2x = v
    _Form("im7_2tx", 1, (4, -3), None, 0),
    _Form("im7_2tx", 4, (3, 1), 1, 1),  # 4x - 3b = v
    _Form("im7_2tx", 3, (4, -1), 1, -1),  # 4a - 3x = v
    _Form("im5_3tx", 1, (2, 1, -2), None, 0),  # also x as b: 2a + x - 2c = v
    _Form("im5_3tx", 1, (3, -1, -1), None, 0),  # also x as b: 3a - x - c = v
    _Form("im5_3tx", 2, (2, 1, -1), 1, 1),  # 2x + b - 2c = v and 2a + b - 2x = v
    _Form("im5_3tx", 3, (1, 1, 1), 0, 1),  # 3x - b - c = v
)


@dataclass(frozen=True)
class _Group:
    """Targets of one form that share one required spacing, sorted by ``t``."""

    t: I64
    slots: I64  # (N, len(coefs)) carrier slots in coefficient order


def _empty(k: int) -> _Group:
    return _Group(np.empty(0, dtype=np.int64), np.empty((0, k), dtype=np.int64))


def _pairs(n: int, unordered: bool) -> tuple[I64, I64]:
    if unordered:
        i, j = np.triu_indices(n, 1)
    else:
        i, j = np.nonzero(~np.eye(n, dtype=bool))
    return i.astype(np.int64), j.astype(np.int64)


class ProductSet:
    """Incrementally maintained IMD state of the placed carriers.

    ``ranges`` are the candidate tuning ranges ``(start_hz, stop_hz)``; ``rules`` are all rules
    that may be added or checked, which fix the tracked kinds (any rule > 0) and the window
    margin (largest IMD spacing). ``add`` and ``remove`` are exact inverses.
    """

    def __init__(self, ranges: Sequence[tuple[int, int]], rules: Iterable[SpacingRules]) -> None:
        if not ranges:
            raise ValueError("at least one tuning range is required")
        for lo, hi in ranges:
            if lo > hi:
                raise ValueError(f"tuning range start {lo} Hz is above its stop {hi} Hz")
        self._ranges = tuple((int(lo), int(hi)) for lo, hi in ranges)
        rules = list(rules)
        self._kinds = frozenset(k for k in KINDS if any(getattr(r, k) > 0 for r in rules))
        self._margin = max((getattr(r, k) for r in rules for k in KINDS), default=0)
        self._forms = tuple((i, f) for i, f in enumerate(_FORMS) if f.kind in self._kinds)
        self._groups: list[dict[int, _Group]] = [{} for _ in _FORMS]
        self._slot_of: dict[CarrierId, int] = {}
        self._id_of: dict[int, CarrierId] = {}
        self._freq_of: dict[int, int] = {}
        self._rules_of: dict[int, SpacingRules] = {}
        self._next_slot = 0
        self._rebuild_dense()

    # ------------------------------------------------------------------ carriers

    def carrier_ids(self) -> tuple[CarrierId, ...]:
        return tuple(self._slot_of)

    def carriers(self) -> tuple[tuple[CarrierId, int], ...]:
        return tuple((cid, self._freq_of[s]) for cid, s in self._slot_of.items())

    def add(self, carrier_hz: int, rules: SpacingRules, carrier_id: CarrierId) -> None:
        """Place a carrier and add every product / target it takes part in."""
        if carrier_id in self._slot_of:
            raise ValueError(f"carrier {carrier_id!r} is already placed")
        self._check_rules(rules)
        slot = self._next_slot
        self._next_slot += 1
        f = int(carrier_hz)
        for index, form in self._forms:
            t, slots, req = self._new_entries(form, f, getattr(rules, form.kind), slot)
            groups = self._groups[index]
            for value in np.unique(req).tolist():
                sel = req == value
                old = groups.get(value, _empty(len(form.coefs)))
                all_t = np.concatenate([old.t, t[sel]])
                order = np.argsort(all_t, kind="stable")
                groups[value] = _Group(all_t[order], np.concatenate([old.slots, slots[sel]])[order])
        self._slot_of[carrier_id] = slot
        self._id_of[slot] = carrier_id
        self._freq_of[slot] = f
        self._rules_of[slot] = rules
        self._rebuild_dense()

    def remove(self, carrier_id: CarrierId) -> None:
        """Undo ``add`` for ``carrier_id`` (``KeyError`` if it is not placed)."""
        slot = self._slot_of.pop(carrier_id)
        del self._id_of[slot], self._freq_of[slot], self._rules_of[slot]
        for index, _ in self._forms:
            groups = self._groups[index]
            for value, group in list(groups.items()):
                keep = ~(group.slots == slot).any(axis=1)
                if keep.all():
                    continue
                if keep.any():
                    groups[value] = _Group(group.t[keep], group.slots[keep])
                else:
                    del groups[value]
        self._rebuild_dense()

    # ------------------------------------------------------------------ inspection

    def products(self) -> tuple[Product, ...]:
        """The stored (window-limited) products of the placed carriers, sorted by frequency."""
        out: list[Product] = []
        for index, form in self._forms:
            if form.victim is not None:
                continue
            for group in self._groups[index].values():
                for t, row in zip(group.t.tolist(), group.slots.tolist(), strict=True):
                    terms = tuple((c, self._id_of[s]) for c, s in zip(form.coefs, row, strict=True))
                    out.append(Product(form.kind, t, terms))
        out.sort(key=lambda p: (p.freq_hz, KINDS.index(p.kind)))
        return tuple(out)

    def entry_count(self) -> int:
        """Total stored targets over all forms (products plus aggressor targets)."""
        return sum(g.t.size for groups in self._groups for g in groups.values())

    def snapshot(self) -> frozenset[tuple[int, int, int, frozenset[tuple[int, CarrierId]]]]:
        """Order-independent view of the whole state, for tests: (form, target, required, terms)."""
        out: set[tuple[int, int, int, frozenset[tuple[int, CarrierId]]]] = set()
        for index, form in self._forms:
            for value, group in self._groups[index].items():
                for t, row in zip(group.t.tolist(), group.slots.tolist(), strict=True):
                    terms = frozenset(
                        (c, self._id_of[s]) for c, s in zip(form.coefs, row, strict=True)
                    )
                    out.add((index, t, value, terms))
        return frozenset(out)

    # ------------------------------------------------------------------ checks

    def nearest_conflict(self, candidate_hz: int, candidate_rules: SpacingRules) -> Conflict | None:
        """The worst rule violation a carrier at ``candidate_hz`` would suffer or cause.

        Worst = largest ``required - actual``, then smallest ``actual``, then rule order
        (carrier, im3_2tx, im3_3tx, im5_2tx, im7_2tx, im5_3tx). ``None`` when it is clean.
        """
        self._check_rules(candidate_rules)
        x = int(candidate_hz)
        self._check_window(np.array([x], dtype=np.int64))
        best: tuple[tuple[int, int, int], Conflict] | None = None

        def offer(rule: str, required: int, actual: int, make: Conflict) -> None:
            nonlocal best
            key = (required - actual, -actual, -_RULE_ORDER.index(rule))
            if best is None or key > best[0]:
                best = (key, make)

        if self._d_freq.size:
            req = np.maximum(self._d_carrier, candidate_rules.carrier)
            dist = np.abs(self._d_freq - x)
            hit = np.flatnonzero(dist < req)
            if hit.size:
                i = int(hit[np.lexsort((dist[hit], -(req[hit] - dist[hit])))[0]])
                sid = self._id_of[int(self._d_slot[i])]
                offer(
                    "carrier",
                    int(req[i]),
                    int(dist[i]),
                    Conflict("carrier", int(req[i]), int(dist[i]), (sid,), None, None),
                )

        for index, form in self._forms:
            own = getattr(candidate_rules, form.kind)
            mx = form.m * x
            for value, group in self._groups[index].items():
                w = max(value, own)
                if w == 0:
                    continue
                lo = int(np.searchsorted(group.t, mx - w, side="right"))
                hi = int(np.searchsorted(group.t, mx + w, side="left"))
                if hi <= lo:
                    continue
                dist = np.abs(group.t[lo:hi] - mx)
                i = lo + int(np.argmin(dist))  # same required spacing in a group
                actual = int(abs(int(group.t[i]) - mx))
                row = group.slots[i].tolist()
                ids = tuple(self._id_of[s] for s in row)
                if form.victim is None:
                    victim: CarrierId | None = None
                    product = int(group.t[i])
                else:
                    victim = ids[form.victim]
                    product = self._freq_of[row[form.victim]] + form.sign * (mx - int(group.t[i]))
                offer(form.kind, w, actual, Conflict(form.kind, w, actual, ids, victim, product))
        return None if best is None else best[1]

    def conflict_mask(
        self, candidates_hz: I64, candidate_rules: SpacingRules
    ) -> npt.NDArray[np.bool_]:
        """Vectorised ``nearest_conflict(f, rules) is not None`` for every candidate."""
        self._check_rules(candidate_rules)
        x = np.asarray(candidates_hz, dtype=np.int64)
        self._check_window(x)
        out = np.zeros(x.shape, dtype=bool)
        if self._d_freq.size:
            req = np.maximum(self._d_carrier, candidate_rules.carrier)
            out |= (np.abs(x[:, None] - self._d_freq[None, :]) < req[None, :]).any(axis=1)
        for index, form in self._forms:
            own = getattr(candidate_rules, form.kind)
            mx = form.m * x
            for value, group in self._groups[index].items():
                w = max(value, own)
                if w == 0 or group.t.size == 0:
                    continue
                lo = np.searchsorted(group.t, mx - w, side="right")
                hi = np.searchsorted(group.t, mx + w, side="left")
                out |= hi > lo
        return out

    # ------------------------------------------------------------------ internals

    def _check_rules(self, rules: SpacingRules) -> None:
        for kind in KINDS:
            value = getattr(rules, kind)
            if value > 0 and kind not in self._kinds:
                raise ValueError(f"{kind} is enabled but this product set does not track it")
            if value > self._margin:
                raise ValueError(
                    f"{kind} spacing {value} Hz exceeds this product set's margin {self._margin} Hz"
                )

    def _check_window(self, x: I64) -> None:
        inside = np.zeros(x.shape, dtype=bool)
        for lo, hi in self._ranges:
            inside |= (x >= lo) & (x <= hi)
        if not inside.all():
            bad = int(x[~inside][0])
            raise ValueError(f"candidate {bad} Hz is outside the product window (tuning ranges)")

    def _rebuild_dense(self) -> None:
        slots = list(self._id_of)
        self._d_slot: I64 = np.array(slots, dtype=np.int64)
        self._d_freq: I64 = np.array([self._freq_of[s] for s in slots], dtype=np.int64)
        self._d_carrier: I64 = np.array([self._rules_of[s].carrier for s in slots], dtype=np.int64)
        self._d_rules: dict[str, I64] = {
            k: np.array([getattr(self._rules_of[s], k) for s in slots], dtype=np.int64)
            for k in self._kinds
        }

    def _new_entries(self, form: _Form, f: int, own: int, slot: int) -> tuple[I64, I64, I64]:
        """Targets of ``form`` that involve the new carrier and the placed ones, window-limited."""
        freqs, rules, slots = self._d_freq, self._d_rules[form.kind], self._d_slot
        coefs = form.coefs
        k = len(coefs)
        t_parts: list[I64] = []
        s_parts: list[I64] = []
        r_parts: list[I64] = []
        for p in range(k):
            if coefs[p] in coefs[:p]:
                continue  # same coefficient as an earlier role: that role already covers it
            others = [q for q in range(k) if q != p]
            if k == 2:
                (q,) = others
                n = freqs.size
                t = coefs[p] * f + coefs[q] * freqs
                rows = np.empty((n, 2), dtype=np.int64)
                rows[:, q] = slots
                req = np.maximum(rules, own)
            else:
                q, r = others
                i, j = _pairs(freqs.size, unordered=coefs[q] == coefs[r])
                n = i.size
                t = coefs[p] * f + coefs[q] * freqs[i] + coefs[r] * freqs[j]
                rows = np.empty((n, 3), dtype=np.int64)
                rows[:, q] = slots[i]
                rows[:, r] = slots[j]
                req = np.maximum(np.maximum(rules[i], rules[j]), own)
            rows[:, p] = slot
            t_parts.append(t)
            s_parts.append(rows)
            r_parts.append(req)
        t = np.concatenate(t_parts)
        rows = np.concatenate(s_parts)
        req = np.concatenate(r_parts)
        keep = np.zeros(t.shape, dtype=bool)
        for lo, hi in self._ranges:
            keep |= (t >= form.m * lo - self._margin) & (t <= form.m * hi + self._margin)
        return t[keep], rows[keep], req[keep]


__all__ = ["KINDS", "CarrierId", "Conflict", "Product", "ProductSet"]
