# Conventions

## Code
- Python ≥ 3.11 syntax, full type hints, `mypy --strict` on `device/`, `core/`, `coord/`.
- ruff for lint and format (line length 100). Imports are absolute (`from opencoord.device import protocol`).
- Frozen dataclasses for value types; numpy arrays for traces (`float64` Hz axis, `float32` dBm).
- Frequencies are `int` Hz internally; helper functions `mhz()`/`khz()` sit only at the edges.
- No print debugging: use the `logging` module (logger per module). The CLI `-v` flag enables debug output.
- Pure modules never import `serial`, `dearpygui`, or do file I/O.

## Tests
- `tests/` mirrors `src/opencoord/`. Use pytest + Hypothesis.
- Hardware tests use `@pytest.mark.hardware` and are skipped unless `OPENCOORD_HARDWARE=1` (and optional
  `OPENCOORD_PORT`) is set.
- Fixtures: raw byte captures from the real device in `tests/fixtures/*.bin`, recorded with
  `opencoord-cli record --raw`, each with a sidecar `.json` describing the model and settings. (The first three,
  `wsub1gplus_*.bin`, predate `record` and were captured with a throwaway pyserial script.)
  `record` taps `SerialLink(raw_sink=...)` (every chunk the reader thread reads, handshake included, host writes
  not included) and writes `<name>.json` next to the `.bin`; it needs a real device (not `--simulator`).
- CLI exit codes: 0 ok, 1 device/connection error, 2 usage. `cli.main(argv, serial_factory=, port_lister=)` is injectable.
- UI smoke test marked `@pytest.mark.ui`; CI runs it under `xvfb-run` on Linux.

## Git
- Conventional-ish messages (`device: parse $z sweeps`, `ci: add nix workflow`).
- Each phase from the plan ends in green CI and a commit.

## Keeping this skill current
- When code diverges from a description here, fix the description in the same commit.
- When a ⚠ item in `device-protocol.md` is verified, remove the ⚠ and note the fixture that proves it.
- When a "(planned)" section is implemented, replace "(planned)" with real file paths and symbol names.
