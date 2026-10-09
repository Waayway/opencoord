# Coordination engine

Status: profiles + spacing (`coord/profiles.py`, `coord/spacing.py`, `io/profile_store.py`) are done; imd, solver (planned).

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
- `ProfileStore(config_dir=None)`: `seed_defaults()` (only when `<config>/spacing/` does not exist, so deletions and
  edits stick), `load_presets()` / `load_profiles(presets)` -> `(items, [LoadIssue(path, message)])` (bad files never
  raise), `save_/delete_preset`, `reset_preset(name)` (built-in only, `KeyError` otherwise), `save_/delete_profile`.
  Files are named by a slug of the name; the name inside the file is authoritative.
- Templates: Generic analog mic, Generic IEM, Generic digital, Fixed-channel set.
- Every rejection records `(rule, required_khz, actual_khz, other_carrier)` so the UI can explain it (planned, solver).

## IMD math (`coord/imd.py`)
Products for carriers `f`:
- 3rd order, 2-transmitter: `2·fi − fj`
- 3rd order, 3-transmitter: `fi + fj − fk`
- 5th order, 2-transmitter: `3·fi − 2·fj`
- 7th order, 2-transmitter: `4·fi − 3·fj`
- 5th order, 3-transmitter (advanced, off by default): `2·fi + fj − 2·fk`, …

**Defaults: 3rd + 5th + 7th on** in every seeded preset.
Products are kept incrementally while the solver places carriers, because recomputing the O(n³) 3-Tx set each time
is too slow. Vectorised with numpy broadcasting. Only products inside the union of tuning ranges ± max spacing are kept.

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
