# Contributing to OpenCoord

Thanks for helping. OpenCoord is GPL-3.0-or-later; by contributing you agree your changes are released
under the same license.

## Development setup

You need [uv](https://docs.astral.sh/uv/) (it manages Python, the virtual environment and `uv.lock`).
Never use `pip install` in this project. On Nix, `nix develop` gives a shell with uv, Python 3.13 and the
GL libraries Dear PyGui needs.

```sh
git clone https://github.com/Waayway/opencoord
cd opencoord
uv sync
uv run opencoord --simulator      # run the app without hardware
```

Add dependencies with `uv add` (runtime dependencies are deliberately few: `dearpygui`, `pyserial`, `numpy`,
`platformdirs`, `tomli-w`; ask before adding another) and commit the changed `uv.lock`.

## Tests and checks

Run all of these before opening a pull request (CI runs them on Linux, Windows and macOS):

```sh
uv run ruff check
uv run ruff format --check     # `uv run ruff format` fixes formatting
uv run mypy src
uv run pytest
```

- Tests mirror `src/opencoord/` under `tests/`. Pure logic is developed test first (pytest + Hypothesis).
- The simulator (`SimulatedLink`) replaces hardware in tests and CI.
- Hardware tests are marked `@pytest.mark.hardware` and skipped unless `OPENCOORD_HARDWARE=1` is set
  (optionally `OPENCOORD_PORT=/dev/ttyUSB0`); run them only with a real RF Explorer attached.
- UI smoke tests are marked `ui` and need a display (`xvfb-run -a uv run pytest -m ui` on Linux).

## Style and architecture

- Python 3.11+, full type hints, ruff (line length 100), absolute imports, `mypy --strict` for
  `opencoord.device`, `opencoord.core` and `opencoord.coord`. UI text is English.
- Frequencies are `int` Hz in code and dBm floats for levels; MHz and kHz only at the UI and file edges.
- Layering: `device/protocol.py`, `device/models.py`, `core/traces.py` and everything in `coord/` are pure (no
  serial, no file I/O, no Dear PyGui). Only `ui/` imports Dear PyGui; only the serial worker touches the port.
- Use the `logging` module, not `print`. Commit messages look like `coord: spacing presets from TOML`.

Read `.claude/skills/opencoord-implementation/SKILL.md` and the reference file for the area you touch before
changing code.

## The skill-update rule

`.claude/skills/opencoord-implementation/` documents how OpenCoord is built (for contributors and coding
agents). **Any commit that changes an implementation detail described there must update the matching file in
the same commit.** Mark hardware-verified items by removing their warning and naming the fixture that proves it.

## Adding things

**A channel plan** (for example another country's TV or PMSE bands): add `src/opencoord/coord/channel_plans/<name>.toml`
following `eu.toml` (`[plan] title`, `[[channels]]` rasters, `[[bands]]` annotations with
`pmse = "allowed" | "forbidden" | "info"`). Cite the regulator's source in a comment at the top of the file
and say how current it is. The legality flags are informational, so be conservative. Add the plan to
`tests/coord/test_channel_plans.py` (it must parse and `channel_at` must give the right channels).

**A device profile**: for devices other people could use, add a TOML file to `profiles/` (see
`profiles/README.md`); `tests/coord/test_community_profiles.py` checks that it parses. Profiles that should
ship inside the app as templates go to `src/opencoord/coord/profile_templates/` instead (open an issue first).

**An RF Explorer model**: add its model code to `MODELS` in `src/opencoord/device/models.py` (name,
min/max frequency hint, `plus=True` for Plus models) with a test in `tests/device/test_models.py`. The device's
own configuration reply always wins over these hints. If you own the device, record a fixture with
`uv run opencoord-cli record --raw tests/fixtures/<model>_...` and describe it in
`device-protocol.md`; models verified this way move from "community-tested" to "hardware-tested" in the README.

## Pull requests

Keep changes focused, include tests, and describe what you verified. For anything that changes the
solver, protocol or file formats, say which tests cover it.

## Releasing (maintainers)

1. Move the `## [x.y.z] - unreleased` heading in `CHANGELOG.md` to a dated one and bump `version` in
   `pyproject.toml` (the release workflow checks that the tag matches it).
2. Tag `vX.Y.Z` and push the tag. `Release` builds everything and creates a **draft** GitHub Release with
   `SHA256SUMS`; `Docker` pushes the image to GHCR.
3. Review the draft and publish it.
