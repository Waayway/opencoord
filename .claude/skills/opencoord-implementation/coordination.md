# Coordination engine

Status: profiles + spacing (`coord/profiles.py`, `coord/spacing.py`, `io/profile_store.py`) and the IMD engine
(`coord/imd.py`) are done; solver (planned).

## Device profiles (`coord/profiles.py`, `coord/spacing.py`, `io/profile_store.py`) (done)
Stored as TOML in `<platformdirs config>/profiles/` (spacing presets in `<config>/spacing/`). Built-in templates ship
as package data in `coord/profile_templates/*.toml` and presets in `coord/spacing_presets/*.toml` (read-only via
`importlib.resources`, like `channel_plans/`; each package has `available()` / `read_text(name)`).
`coord/profiles.py` and `coord/spacing.py` are **pure** (parse/validate from text or dict, serialise with `tomli_w`);
all file access is in `io/profile_store.py` (`ProfileStore`, atomic writes through `io/atomic.py`).
```toml
[profile]
name = "Generic UHF handheld 823-832"
kind = "mic"                 # mic | iem | other
tuning = [[823.0, 832.0]]    # MHz ranges (converted to Hz on load); needs step_khz
step_khz = 25
spacing = "generic-analog"   # REQUIRED preset name; must exist in the preset registry passed to the parser

[spacing]                    # optional per-profile OVERRIDES of the preset; kHz; 0 = rule disabled
im3_2tx = 100                # fields: carrier im3_2tx im3_3tx im5_2tx im7_2tx im5_3tx
```
- Exactly **one** frequency source: `tuning`+`step_khz`, a flat `channels = [MHz...]` list, or `[[groups]]`
  (`name`, `channels`). Mixing them is a validation error (not "channels overrides tuning").
- Python: `DeviceProfile(name, kind, spacing_preset, tuning: tuple[TuningRange], step_hz, channels, groups,
  spacing_overrides: PartialSpacing)`; `parse_profile(text, preset_names)` / `profile_from_dict` /
  `profile_to_toml`; errors are `ProfileError(ValueError)` naming field and value (unknown preset lists the available
  ones; tuning is capped at 100 000 candidates). `candidates(profile) -> tuple[Candidate(freq_hz, groups)]`: sorted,
  unique int Hz (tuning ranges include both ends); `groups` is the membership tuple (empty if ungrouped).
  `builtin_templates()` -> dict keyed by file stem (`generic-analog-mic`, `generic-iem`, `generic-digital`,
  `fixed-channel-set`).
- Spacing is **data, never constants**. `SpacingRules(carrier, im3_2tx, im3_3tx, im5_2tx, im7_2tx, im5_3tx)` in int Hz
  (TOML kHz, 0 = off); `SpacingPreset(name, description, rules)`; preset file = `[preset] name, description` +
  `[spacing]`. Seeded presets (starting points, user-configurable; kHz carrier/im3_2tx/im3_3tx/im5_2tx/im7_2tx,
  im5_3tx off): generic-analog 350/100/50/50/50, conservative 400/150/75/75/75, iem 600/200/100/100/100,
  digital 200/50/25/25/25. `builtin_presets()` returns them.
- **Resolution** (`resolve_profile_rules(preset_rules, profile_overrides, run)`): preset -> profile overrides (set
  fields replace) -> `RunOverride(scale=1.0, values=PartialSpacing())`: its `values` replace fields, then `scale`
  multiplies every field of the result (rounded to Hz; must be > 0). For a pair of devices
  `SpacingRules.resolve(a, b)` (= `a.resolve(b)`) is the per-field max (stricter); commutative, >= both.
- `ProfileStore(config_dir=None)`: `seed_defaults()` (writes missing built-ins once, then a `.seeded` marker in
  `<config>/spacing/`, so deletions and edits stick and a prior `save_preset` is never overwritten),
  `load_presets()` / `load_profiles(presets)` -> `(items, [LoadIssue(path, message)])` (bad files and files with a
  duplicate internal name become issues, never raise), `save_/delete_preset`, `rename_preset(old_name, preset)`,
  `rename_profile(old_name, profile)` (save new, remove old file; `FileExistsError` on a name clash),
  `reset_preset(name)` (built-in only, `KeyError` otherwise), `save_/delete_profile`.
  Files are `<slug>.toml` (NFC, casefolded, Unicode word chars kept); the name inside the file is authoritative and
  files are found by it. A new name whose slug is taken by a different name gets a numeric suffix (`-2`, ...).
- Unknown keys in `[profile]`, `[[groups]]`, `[spacing]` and the top level are validation errors; numbers are bounded
  (<= 1e6 MHz, step/spacing <= 1e9 kHz) so bad files cannot raise from `round()`.
- Templates: Generic analog mic, Generic IEM, Generic digital, Fixed-channel set.
- Every rejection records `(rule, required_khz, actual_khz, other_carrier)` so the UI can explain it (planned, solver).

## IMD math (`coord/imd.py`) (done)
Pure, exact `int64` Hz arithmetic (no float rounding). Kinds = the `SpacingRules` IMD fields (`imd.KINDS`), products
always over **distinct** carriers:
- `im3_2tx` 3rd order, 2-Tx: `2a − b` (ordered pairs)
- `im3_3tx` 3rd order, 3-Tx: `a + b − c` (`{a, b}` unordered, `c` the third)
- `im5_2tx` 5th order, 2-Tx: `3a − 2b`
- `im7_2tx` 7th order, 2-Tx: `4a − 3b`
- `im5_3tx` 5th order, 3-Tx (advanced, off by default): **both** 5th-order 3-carrier forms whose coefficients sum
  to 1 (so they land near the carriers): `2a + b − 2c` (6 orderings per triple) and `3a − b − c` (3 per triple).

**Defaults: 3rd + 5th + 7th on** in every seeded preset.

**Rules (decided):** a product and the carrier it lands on (the victim) conflict when `|product − victim| < required`,
`required` = the **max** of that kind over the victim's rules and every source carrier's rules (stricter of all
involved). A carrier is never the victim of its own product. Carrier–carrier: `|a − b| < resolve(a, b).carrier`.

`ProductSet(ranges, rules)` (ranges = candidate tuning ranges `(start_hz, stop_hz)`; `rules` = every
`SpacingRules` that will be added or checked; it fixes the tracked kinds (any value > 0) and the window margin =
largest IMD spacing; untracked kinds or bigger values later raise `ValueError`):
- `add(carrier_hz, rules, carrier_id)` / `remove(carrier_id)` are exact inverses (backtracking); each carrier
  keeps its own resolved rules. Ids are any hashable; duplicate id → `ValueError`, unknown → `KeyError`.
- Internally each **check form** keeps sorted `int64` targets `T` grouped by required spacing; a candidate `x`
  violates an entry when `|m·x − T| < max(required, candidate rule)`. `m = 1` forms are the products (x is hit);
  `m > 1` forms cover products that x would **create** landing on a placed carrier (x as aggressor), e.g.
  `2x − b = v` ⇔ `|2x − (b + v)|`. Aggressor roles with coefficient ±1 coincide with an `m = 1` form and share it.
  13 forms in `_FORMS`. New entries per `add` are vectorised (pairs/triples via `triu_indices` / off-diagonal
  index arrays, O(n²) per added carrier for 3-Tx).
- Range limiting: a target is kept only if `m·lo − margin ≤ T ≤ m·hi + margin` for some range; therefore
  candidates must lie inside the ranges (`ValueError` "outside the product window" otherwise). Locked carriers
  may lie anywhere.
- `nearest_conflict(candidate_hz, rules) -> Conflict | None`: carrier spacing vs all placed, products on the
  candidate, and products the candidate creates on placed carriers. Returns the worst: largest
  `required − actual`, then smallest `actual`, then rule order (carrier, im3_2tx, im3_3tx, im5_2tx, im7_2tx,
  im5_3tx). `Conflict(rule, required_hz, actual_hz, sources, victim, product_hz)`: `sources` = placed carriers
  involved (incl. the victim), `victim` = `None` if the candidate is hit, `product_hz` = `None` for carrier rule.
- `conflict_mask(candidates_hz, rules) -> bool array`: vectorised `nearest_conflict(...) is not None`
  (searchsorted per form/group); the solver filters with this and calls `nearest_conflict` only to explain.
- `products()` (m = 1 entries as `Product(kind, freq_hz, terms=((coef, id), ...))`, `.sources`, `.order`),
  `carriers()`, `carrier_ids()`, `entry_count()`, `snapshot()` (order-independent state for tests).
- Measured (tests/coord/test_imd.py bench, 470–694 MHz, 8961 candidates at 25 kHz, 40 carriers first-fit):
  default orders 0.08 s total (mask 1.3 ms/call, nearest_conflict ~25 µs); with im5_3tx 0.12 s, ~115k entries.
- Tests: Hypothesis incremental == from-scratch (products vs an itertools brute force, and full `snapshot()` vs a
  fresh set), and `nearest_conflict` / `conflict_mask` vs a brute-force oracle on ≤ 6 carriers on a small grid.

## Solver (`coord/solver.py`)
Input: `CoordinationRequest`, which contains
- profiles × quantities
- locked carriers
- the scan trace
- exclusions (TV channels, zones)
- the occupancy threshold in dB above the noise floor
- the guard bandwidth
- a time budget

Steps:
1. Build the candidate grid per profile.
2. Drop excluded and occupied candidates.
3. Rank candidates by scan level.
4. Assign the most-constrained device first, with backtracking and incremental product sets.
5. Stop at full assignment or when the time budget runs out (best partial result).

Output: a `Plan` with assignments, unassigned devices with reasons, backups per profile, and a conflict report.
`check(plan)` validates a hand-made plan with the same rules.

Invariant tested with Hypothesis: **no returned plan violates any active rule.**

## Channel plans (`coord/channel_plans/*.toml`) (done)
Pure data, parsed by the pure `parse_plan(text, name) -> ChannelPlan` (`channel_plans/model.py`; `ValueError` on
bad data). `channel_plans/__init__.py` has `available()` / `load(name)` which only read package resources
(`importlib.resources`; the only file access in `coord/`; `FileNotFoundError` for an unknown name).
- File format (MHz in the file, Hz `int` in code): `[plan] title`; `[[channels]]` rasters (`first`, `last`,
  `centre_base_mhz`, `width_mhz`, optional `pmse`: channel N has centre `centre_base_mhz + width_mhz * N`);
  `[[bands]]` annotations (`start_mhz`, `stop_mhz`, `pmse = "allowed" | "forbidden" | "info"`, `note`).
- `ChannelPlan(name, title, channels, bands)`: `Channel(number, start_hz, stop_hz, pmse)` (+ `centre_hz`; edges are
  `start <= f < stop`), `BandAnnotation`, `channel_at(freq_hz)`, `shaded_spans()` (runs of touching channels with
  the same flag merged, then the bands; used by the overlay).

`eu.toml` holds the DVB-T channels 21–48 (centre `306 + 8·N` MHz, ch21 = 470–478; all `allowed`: TV white space,
licence dependent), plus bands: 694–790 `forbidden` ("700 MHz mobile band (not for PMSE in NL)"), 790–823
`forbidden`, 823–832 `allowed` ("Mic duplex gap (823-832 MHz)"), 832–862 `forbidden`, 863–865 `allowed`
("Licence-free (863-865 MHz)"), 1785–1805 `info` ("PMSE 1785-1805 MHz (licence)"). The file carries a comment
that legality must be verified against the current RDI / Agentschap Telecom rules.
The solver skips `forbidden` bands unless `CoordinationRequest.allow_forbidden=True`. Plans that use them carry
`Plan.warnings`, which every exporter must print. The app never hard-blocks.

## Groups/banks
A profile may define `[[groups]]` (`name`, `channels`) instead of a flat `channels` list. When the
`prefer_single_group` option is on (the default), the solver tries to keep all devices of a profile in one group
before falling back to mixing groups.
