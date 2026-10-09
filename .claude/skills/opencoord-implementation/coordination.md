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
im5_2tx = 0
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
- 5th order: `3·fi − 2·fj`

Vectorised with numpy broadcasting. Only products inside the union of tuning ranges ± max spacing are kept.

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

## Channel plans (`coord/channel_plans/*.toml`)
Pure data:
- region name and raster
- a channel table (number → Hz edges)
- band annotations (`pmse = "allowed" | "forbidden" | "info"`, note)

`eu.toml` holds the DVB-T channels 21–48, plus annotations for 694–790, 823–832, 863–865 and 1785–1805.
The app annotates and warns; it never blocks.
