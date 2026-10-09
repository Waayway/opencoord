# Coordination engine (planned)

## Device profiles (`coord/profiles.py`)
Stored as TOML in `<config>/profiles/`. Built-in templates ship in the package.
```toml
[profile]
name = "Generic UHF handheld 823-832"
kind = "mic"                 # mic | iem | other
tuning = [[823.0, 832.0]]    # MHz ranges (converted to Hz on load)
step_khz = 25
channels = []                # optional fixed list (MHz); overrides tuning+step when non-empty
spacing = "generic-analog"   # preset name, or an inline [spacing] table

[spacing]                    # only when custom; all kHz; 0 = rule disabled
carrier = 350
im3_2tx = 100
im3_3tx = 50
im5_2tx = 50
im7_2tx = 50
im5_3tx = 0                  # advanced, off by default
```
- Templates: Generic analog mic, Generic IEM, Generic digital, Fixed-channel set.
- Spacing is **data, never constants** (`coord/spacing.py`). Presets are TOML files in `<config>/spacing/`, seeded
  from package defaults (`generic-analog`, `conservative`, `iem`, `digital`) on first run and editable in the UI.
- Resolution: run override → profile `[spacing]` overrides → profile's `spacing` preset.
  `SpacingRules.resolve(a, b)` returns the stricter value per rule for a device pair.
- Every rejection records `(rule, required_khz, actual_khz, other_carrier)` so the UI can explain it.

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
