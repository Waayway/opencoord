"""IMD engine: products, incremental ProductSet, conflict checks (with brute-force oracles)."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Hashable, Sequence

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opencoord.coord.imd import KINDS, Conflict, Product, ProductSet
from opencoord.coord.spacing import SpacingRules

KHZ = 1000
MHZ = 1_000_000
ANALOG = SpacingRules(350 * KHZ, 100 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ, 0)
ALL_ON = SpacingRules(350 * KHZ, 100 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ, 50 * KHZ)
WIDE = [(400 * MHZ, 900 * MHZ)]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- independent brute force

# Written independently of the module: every product form per rule, as coefficient tuples
# applied to *distinct* carriers.
BRUTE_FORMS: dict[str, list[tuple[int, ...]]] = {
    "im3_2tx": [(2, -1)],
    "im3_3tx": [(1, 1, -1)],
    "im5_2tx": [(3, -2)],
    "im7_2tx": [(4, -3)],
    "im5_3tx": [(2, 1, -2), (3, -1, -1)],
}
RULE_ORDER = ("carrier", "im3_2tx", "im3_3tx", "im5_2tx", "im7_2tx", "im5_3tx")

Carrier = tuple[Hashable, int, SpacingRules]


def brute_products(
    carriers: Sequence[Carrier], kinds: Sequence[str]
) -> list[tuple[str, int, frozenset[tuple[int, Hashable]]]]:
    """All products (kind, freq, terms) of distinct carriers, deduplicated by their terms."""
    seen: set[tuple[str, int, frozenset[tuple[int, Hashable]]]] = set()
    for kind in kinds:
        for coefs in BRUTE_FORMS[kind]:
            for combo in itertools.permutations(carriers, len(coefs)):
                freq = sum(c * f for c, (_, f, _) in zip(coefs, combo, strict=True))
                terms = frozenset((c, cid) for c, (cid, _, _) in zip(coefs, combo, strict=True))
                seen.add((kind, freq, terms))
    return list(seen)


def in_window(freq: int, ranges: Sequence[tuple[int, int]], margin: int) -> bool:
    return any(lo - margin <= freq <= hi + margin for lo, hi in ranges)


def brute_conflict(
    placed: Sequence[Carrier], x_hz: int, x_rules: SpacingRules
) -> tuple[str, int, int] | None:
    """Worst violation (rule, required, actual) that candidate X causes or suffers (brute force)."""
    x: Carrier = ("__X__", x_hz, x_rules)
    everyone = [*placed, x]
    found: list[tuple[str, int, int]] = []
    for _, f, r in placed:
        req = max(r.carrier, x_rules.carrier)
        if abs(f - x_hz) < req:
            found.append(("carrier", req, abs(f - x_hz)))
    for kind, forms in BRUTE_FORMS.items():
        for coefs in forms:
            for combo in itertools.permutations(everyone, len(coefs)):
                freq = sum(c * f for c, (_, f, _) in zip(coefs, combo, strict=True))
                ids = {cid for cid, _, _ in combo}
                for victim in everyone:
                    if victim[0] in ids or ("__X__" not in ids and victim[0] != "__X__"):
                        continue
                    involved = [*combo, victim]
                    req = max(getattr(r, kind) for _, _, r in involved)
                    actual = abs(freq - victim[1])
                    if actual < req:
                        found.append((kind, req, actual))
    if not found:
        return None
    return max(found, key=lambda t: (t[1] - t[2], -t[2], -RULE_ORDER.index(t[0])))


def as_terms(p: Product) -> frozenset[tuple[int, Hashable]]:
    return frozenset(p.terms)


# ---------------------------------------------------------------- products


def test_two_carrier_products_have_the_textbook_values() -> None:
    ps = ProductSet(WIDE, [ANALOG])
    ps.add(600 * MHZ, ANALOG, "A")
    ps.add(601 * MHZ, ANALOG, "B")
    got = {(p.kind, p.freq_hz, as_terms(p)) for p in ps.products()}
    a, b = 600 * MHZ, 601 * MHZ
    assert got == {
        ("im3_2tx", 2 * a - b, frozenset({(2, "A"), (-1, "B")})),
        ("im3_2tx", 2 * b - a, frozenset({(2, "B"), (-1, "A")})),
        ("im5_2tx", 3 * a - 2 * b, frozenset({(3, "A"), (-2, "B")})),
        ("im5_2tx", 3 * b - 2 * a, frozenset({(3, "B"), (-2, "A")})),
        ("im7_2tx", 4 * a - 3 * b, frozenset({(4, "A"), (-3, "B")})),
        ("im7_2tx", 4 * b - 3 * a, frozenset({(4, "B"), (-3, "A")})),
    }
    product = next(p for p in ps.products() if p.freq_hz == 599 * MHZ)
    assert product.sources == ("A", "B")
    assert product.order == 3


def test_three_carrier_products_and_advanced_fifth_order() -> None:
    ps = ProductSet(WIDE, [ALL_ON])
    for cid, f in (("A", 600), ("B", 603), ("C", 610)):
        ps.add(f * MHZ, ALL_ON, cid)
    three = sorted(p.freq_hz // MHZ for p in ps.products() if p.kind == "im3_3tx")
    # a + b - c for each unordered {a, b} and the third carrier c
    assert three == sorted([600 + 603 - 610, 600 + 610 - 603, 603 + 610 - 600])
    adv = [p for p in ps.products() if p.kind == "im5_3tx"]
    assert len(adv) == 9  # 6 orderings of 2a+b-2c and 3 of 3a-b-c
    assert {(2, "A"), (1, "B"), (-2, "C")} in [set(p.terms) for p in adv]
    assert {(3, "A"), (-1, "B"), (-1, "C")} in [set(p.terms) for p in adv]
    assert all(p.order == 5 for p in adv)


def test_disabled_kinds_are_not_computed() -> None:
    ps = ProductSet(WIDE, [ANALOG])
    for cid, f in (("A", 600), ("B", 603), ("C", 610)):
        ps.add(f * MHZ, ANALOG, cid)
    assert {p.kind for p in ps.products()} == {"im3_2tx", "im3_3tx", "im5_2tx", "im7_2tx"}


def test_products_outside_the_window_are_dropped() -> None:
    rules = SpacingRules(0, 1 * MHZ, 0, 0, 0, 0)
    ps = ProductSet([(600 * MHZ, 610 * MHZ)], [rules])
    ps.add(600 * MHZ, rules, "A")
    ps.add(605 * MHZ, rules, "B")
    # 2A-B = 595 (outside 599..611), 2B-A = 610 (inside)
    assert [p.freq_hz for p in ps.products()] == [610 * MHZ]
    ps.add(606 * MHZ + 500 * KHZ, rules, "C")
    freqs = {p.freq_hz for p in ps.products()}
    assert all(599 * MHZ <= f <= 611 * MHZ for f in freqs)
    assert 2 * 606_500_000 - 605 * MHZ in freqs  # 608 MHz


def test_remove_is_the_exact_inverse_of_add() -> None:
    ps = ProductSet(WIDE, [ALL_ON])
    for cid, f in (("A", 600), ("B", 603), ("C", 610)):
        ps.add(f * MHZ, ALL_ON, cid)
    before = ps.snapshot()
    ps.add(615 * MHZ, ALL_ON, "D")
    assert ps.snapshot() != before
    ps.remove("D")
    assert ps.snapshot() == before
    assert ps.carrier_ids() == ("A", "B", "C")


# ---------------------------------------------------------------- conflicts


def test_carrier_spacing_uses_the_stricter_rule() -> None:
    loose = SpacingRules(carrier=200 * KHZ)
    strict = SpacingRules(carrier=400 * KHZ)
    ps = ProductSet(WIDE, [loose, strict])
    ps.add(600 * MHZ, strict, "A")
    c = ps.nearest_conflict(600 * MHZ + 300 * KHZ, loose)
    assert c == Conflict(
        rule="carrier",
        required_hz=400 * KHZ,
        actual_hz=300 * KHZ,
        sources=("A",),
        victim=None,
        product_hz=None,
    )
    assert ps.nearest_conflict(600 * MHZ + 400 * KHZ, loose) is None


def test_candidate_hit_by_existing_product() -> None:
    ps = ProductSet(WIDE, [ANALOG])
    ps.add(600 * MHZ, ANALOG, "A")
    ps.add(601 * MHZ, ANALOG, "B")
    c = ps.nearest_conflict(599 * MHZ + 40 * KHZ, ANALOG)  # 2A-B = 599
    assert c is not None
    assert (c.rule, c.required_hz, c.actual_hz) == ("im3_2tx", 100 * KHZ, 40 * KHZ)
    assert set(c.sources) == {"A", "B"}
    assert c.victim is None
    assert c.product_hz == 599 * MHZ


def test_candidate_creates_product_on_placed_carrier() -> None:
    rules = SpacingRules(0, 100 * KHZ, 0, 0, 0, 0)
    ps = ProductSet(WIDE, [rules])
    ps.add(600 * MHZ, rules, "B")
    ps.add(610 * MHZ, rules, "V")
    # 2x - B lands on V when x = 605 MHz; x = 605.03 gives 2x - B = 610.06 (60 kHz off V)
    c = ps.nearest_conflict(605 * MHZ + 30 * KHZ, rules)
    assert c is not None
    assert (c.rule, c.required_hz, c.actual_hz) == ("im3_2tx", 100 * KHZ, 60 * KHZ)
    assert c.victim in {"B", "V"}
    assert set(c.sources) == {"B", "V"}
    assert c.product_hz is not None
    assert abs(c.product_hz - (610 * MHZ if c.victim == "V" else 600 * MHZ)) == 60 * KHZ


def test_product_rule_is_the_strictest_of_candidate_and_sources() -> None:
    off = SpacingRules()
    strict = SpacingRules(0, 100 * KHZ, 0, 0, 0, 0)
    ps = ProductSet(WIDE, [off, strict])
    ps.add(600 * MHZ, strict, "A")
    ps.add(601 * MHZ, off, "B")
    c = ps.nearest_conflict(599 * MHZ + 50 * KHZ, off)
    assert c is not None
    assert (c.rule, c.required_hz) == ("im3_2tx", 100 * KHZ)


def test_all_rules_off_never_conflict() -> None:
    off = SpacingRules()
    ps = ProductSet(WIDE, [off])
    ps.add(600 * MHZ, off, "A")
    ps.add(601 * MHZ, off, "B")
    assert ps.nearest_conflict(599 * MHZ, off) is None
    assert ps.nearest_conflict(600 * MHZ, off) is None


def test_worst_violation_wins() -> None:
    ps = ProductSet(WIDE, [ANALOG])
    ps.add(600 * MHZ, ANALOG, "A")
    ps.add(601 * MHZ, ANALOG, "B")
    # at 599.0 the 3rd-order product hits dead on; carrier spacing is fine (1 MHz)
    c = ps.nearest_conflict(599 * MHZ, ANALOG)
    assert c is not None
    assert (c.rule, c.actual_hz) == ("im3_2tx", 0)


def test_conflict_mask_matches_nearest_conflict() -> None:
    ps = ProductSet([(598 * MHZ, 604 * MHZ)], [ANALOG])
    ps.add(600 * MHZ, ANALOG, "A")
    ps.add(601 * MHZ, ANALOG, "B")
    cands = np.arange(598 * MHZ, 604 * MHZ + 1, 25 * KHZ, dtype=np.int64)
    mask = ps.conflict_mask(cands, ANALOG)
    expect = [ps.nearest_conflict(int(f), ANALOG) is not None for f in cands]
    assert mask.tolist() == expect
    assert any(expect) and not all(expect)


def test_errors() -> None:
    ps = ProductSet([(600 * MHZ, 610 * MHZ)], [ANALOG])
    ps.add(600 * MHZ, ANALOG, "A")
    with pytest.raises(ValueError, match="A"):
        ps.add(601 * MHZ, ANALOG, "A")
    with pytest.raises(KeyError):
        ps.remove("nope")
    with pytest.raises(ValueError, match="window"):
        ps.nearest_conflict(700 * MHZ, ANALOG)
    with pytest.raises(ValueError, match="window"):
        ps.conflict_mask(np.array([605 * MHZ, 700 * MHZ], dtype=np.int64), ANALOG)
    with pytest.raises(ValueError, match="im5_3tx"):
        ps.nearest_conflict(605 * MHZ, ALL_ON)  # kind not tracked by this set
    with pytest.raises(ValueError, match="im3_2tx"):
        ps.add(605 * MHZ, SpacingRules(0, 200 * KHZ, 0, 0, 0, 0), "B")  # beyond margin
    with pytest.raises(ValueError, match="range"):
        ProductSet([(610 * MHZ, 600 * MHZ)], [ANALOG])


def test_kinds_constant() -> None:
    assert KINDS == ("im3_2tx", "im3_3tx", "im5_2tx", "im7_2tx", "im5_3tx")


# ---------------------------------------------------------------- properties

GRID = 40
SMALL_RANGES = [(5, 35)]
rule_values = st.integers(min_value=0, max_value=3)
small_rules = st.builds(
    SpacingRules, rule_values, rule_values, rule_values, rule_values, rule_values, rule_values
)
ops = st.lists(
    st.one_of(
        st.tuples(st.just("add"), st.integers(0, GRID), small_rules),
        st.tuples(st.just("remove"), st.integers(0, 7), st.just(SpacingRules())),
    ),
    max_size=14,
)


@settings(max_examples=150, deadline=None)
@given(ops=ops, pool=st.lists(small_rules, min_size=1, max_size=3))
def test_incremental_equals_from_scratch(
    ops: list[tuple[str, int, SpacingRules]], pool: list[SpacingRules]
) -> None:
    all_rules = [r for _, _, r in ops] + pool
    ps = ProductSet(SMALL_RANGES, all_rules)
    placed: dict[int, tuple[int, SpacingRules]] = {}
    next_id = 0
    for op, value, rules in ops:
        if op == "add":
            if len(placed) >= 6:
                continue
            ps.add(value, rules, next_id)
            placed[next_id] = (value, rules)
            next_id += 1
        elif placed:
            victim = sorted(placed)[value % len(placed)]
            ps.remove(victim)
            del placed[victim]
    carriers = [(cid, f, r) for cid, (f, r) in placed.items()]
    margin = max(max(getattr(r, k) for k in KINDS) for r in all_rules)
    kinds = [k for k in KINDS if any(getattr(r, k) > 0 for r in all_rules)]
    expect = [p for p in brute_products(carriers, kinds) if in_window(p[1], SMALL_RANGES, margin)]
    got = [(p.kind, p.freq_hz, as_terms(p)) for p in ps.products()]
    assert len(got) == len(set(got))
    assert set(got) == set(expect)
    fresh = ProductSet(SMALL_RANGES, all_rules)
    for cid, f, r in carriers:
        fresh.add(f, r, cid)
    assert fresh.snapshot() == ps.snapshot()


@settings(max_examples=300, deadline=None)
@given(
    placed=st.lists(st.tuples(st.integers(0, GRID), small_rules), min_size=0, max_size=6),
    cand=st.integers(SMALL_RANGES[0][0], SMALL_RANGES[0][1]),
    cand_rules=small_rules,
)
def test_nearest_conflict_matches_brute_force(
    placed: list[tuple[int, SpacingRules]], cand: int, cand_rules: SpacingRules
) -> None:
    carriers: list[Carrier] = [(i, f, r) for i, (f, r) in enumerate(placed)]
    ps = ProductSet(SMALL_RANGES, [cand_rules, *(r for _, r in placed)])
    for cid, f, r in carriers:
        ps.add(f, r, cid)
    expect = brute_conflict(carriers, cand, cand_rules)
    got = ps.nearest_conflict(cand, cand_rules)
    if expect is None:
        assert got is None
    else:
        assert got is not None
        assert (got.rule, got.required_hz, got.actual_hz) == expect
        if got.product_hz is not None:
            victim_hz = cand if got.victim is None else placed[int(str(got.victim))][0]
            assert abs(got.product_hz - victim_hz) == got.actual_hz
    mask = ps.conflict_mask(np.array([cand], dtype=np.int64), cand_rules)
    assert bool(mask[0]) == (expect is not None)


# ---------------------------------------------------------------- performance


@pytest.mark.parametrize(
    ("rules", "must_fit"), [(ANALOG, True), (ALL_ON, False)], ids=["default-orders", "with-im5_3tx"]
)
def test_forty_carriers_with_ten_thousand_candidates_is_fast(
    rules: SpacingRules, must_fit: bool
) -> None:
    lo, hi = 470 * MHZ, 694 * MHZ
    cands = np.arange(lo, hi + 1, 25 * KHZ, dtype=np.int64)  # 8961 candidates
    ps = ProductSet([(lo, hi)], [rules])
    t_mask = t_add = 0.0
    placed = forced = 0
    t0 = time.perf_counter()
    while placed < 40:
        s = time.perf_counter()
        free = cands[~ps.conflict_mask(cands, rules)]
        t_mask += time.perf_counter() - s
        if free.size:
            f = int(free[0])  # first fit
        else:  # band full (5th-order 3Tx is strict): keep placing so the timing covers 40
            f = int(cands[(placed * 997) % cands.size])
            forced += 1
        s = time.perf_counter()
        ps.add(f, rules, placed)
        t_add += time.perf_counter() - s
        placed += 1
    s = time.perf_counter()
    for f in cands[:: max(1, cands.size // 500)]:
        ps.nearest_conflict(int(f), rules)
    t_nearest = (time.perf_counter() - s) / len(cands[:: max(1, cands.size // 500)])
    total = time.perf_counter() - t0
    log.warning(
        "imd bench %s: 40 carriers, %d candidates: total %.3fs, mask %.3fs (%.1f ms/call), "
        "add %.3fs, nearest_conflict %.0f us/call, %d stored entries, %d forced",
        rules,
        cands.size,
        total,
        t_mask,
        1000 * t_mask / 40,
        t_add,
        1e6 * t_nearest,
        ps.entry_count(),
        forced,
    )
    assert total < 5.0
    if not must_fit:
        return
    assert forced == 0
    # every placed carrier is clean against the others
    snapshot = [(c, f) for c, f in ps.carriers()]
    for cid, f in snapshot:
        ps.remove(cid)
        assert ps.nearest_conflict(f, rules) is None
        ps.add(f, rules, cid)
