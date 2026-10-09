# OpenCoord v0.1 — Implementation Tasks

Spec: `plans/2026-10-09-opencoord-plan.md` (design, binding). How-to: `.claude/skills/opencoord-implementation/`
(read `SKILL.md` + the reference file(s) relevant to your task **before coding**, and update them in the same
commit when your implementation adds or changes details; replace "(planned)" with real paths/symbols).

## Global Constraints
- Python package `opencoord`, `src/` layout, `requires-python = ">=3.11"`, `.python-version` = `3.13`.
- **uv only**: the binary is at `~/.local/bin/uv` (prepend `export PATH="$HOME/.local/bin:$PATH"`). Never pip.
  `uv.lock` committed. Use `uv add` / `uv run`.
- Runtime deps exactly: `dearpygui`, `pyserial`, `numpy`, `platformdirs`, `tomli-w` (add others only if a task says so).
  Dev group: `pytest`, `hypothesis`, `ruff`, `mypy`, `pyinstaller`.
- Layering (SKILL.md golden rules): `device/protocol.py`, `device/models.py`, `core/traces.py`, `coord/*` are pure —
  no serial, no file I/O, no dearpygui imports. Only `ui/` imports dearpygui.
- Frequencies internally `int` Hz (numpy `float64` Hz arrays for traces); levels dBm floats; MHz/kHz only at UI/IO edges.
- TDD for pure modules: write failing test, see it fail, implement, see it pass. Record RED/GREEN in the report.
- `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` and `uv run pytest` must pass before commit.
  mypy strict for `opencoord.device`, `opencoord.core`, `opencoord.coord`.
- UI English only. License GPL-3.0-or-later. Repo `github.com/Waayway/opencoord`, branch `feat/v0.1`.
- Real hardware: RF Explorer WSUB1G PLUS SLIM on `/dev/ttyUSB0` (CP2102N, `10c4:ea60`); user has access.
  Hardware tests: `@pytest.mark.hardware`, skipped unless `OPENCOORD_HARDWARE=1`.
- Do not push, tag, or publish. Commit on the current branch only. Commit messages end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` followed by
  `Claude-Session: https://claude.ai/code/session_01F7NJJ67wr6sKXpG8eBqLbz`.
- Never run `git clean`; `.superpowers/` is scratch for the controller.

---

### Task 1: uv project skeleton
Create the Python project (plan §8.1, skill `tooling-and-packaging.md`, `architecture.md`, `conventions.md`).
- `uv init --package` style layout: `pyproject.toml` (name `opencoord`, version `0.1.0.dev0`, description, GPL-3.0-or-later,
  classifiers, scripts `opencoord = "opencoord.ui.app:main"`, `opencoord-cli = "opencoord.cli:main"`), `.python-version`
  (`3.13`), `uv.lock`.
- Deps per Global Constraints. Tool config in `pyproject.toml`: ruff (line-length 100, sensible rule set incl. `I`, `UP`, `B`),
  mypy (strict for device/core/coord packages), pytest (markers `hardware`, `ui`; `addopts` skips nothing — skipping is
  done by a `conftest.py` hook that skips `hardware` unless `OPENCOORD_HARDWARE=1`).
- Package skeleton with `__init__.py` (exposes `__version__` via `importlib.metadata`) for `opencoord`, `device`, `core`,
  `coord`, `io` (name it `opencoord/io` — ensure no stdlib shadowing problems; absolute imports only), `ui`, `ui/panels`.
  `__main__.py` calls `opencoord.ui.app.main`.
- `src/opencoord/ui/app.py`: minimal Dear PyGui window titled "OpenCoord <version>" with `main(argv=None)` using argparse:
  `--version`, `--simulator` (accepted, no-op for now), `--smoke-frames N` (render N frames then exit 0; used by CI and
  packaging smoke tests). Manual render loop per skill `ui.md`.
- `src/opencoord/cli.py`: `main(argv=None)` with `--version` and subcommand placeholders printing "not implemented yet" (exit 2).
- Tests: `tests/test_version.py` (version string, `opencoord-cli --version` via `main`), `tests/ui/test_smoke.py` marked `ui`
  that runs `app.main(["--smoke-frames","5"])` (skip if no display: neither `DISPLAY` nor `WAYLAND_DISPLAY`).
- Files: `LICENSE` (full GPL-3.0 text, fetch from `https://www.gnu.org/licenses/gpl-3.0.txt`), `README.md` (short: what it is,
  status "pre-alpha", dev quickstart with uv, Linux serial permission note), `.gitignore` (Python, `.venv`, `dist/`, `build/`,
  `result`, `.superpowers/`), `CHANGELOG.md` (Unreleased).
- Update skill `tooling-and-packaging.md` + `architecture.md` to match (remove "(planned)" where now real).
- Done when: `uv sync`, ruff, mypy, pytest (incl. ui smoke locally under the Wayland/X display) all pass; `uv run opencoord --version` prints the version.

### Task 2: CI workflow
`.github/workflows/ci.yml` per plan §8.5: on push + pull_request; `astral-sh/setup-uv` (enable cache); jobs:
`lint` (ruff check, ruff format --check, mypy) on ubuntu-latest; `test` matrix os {ubuntu-latest, windows-latest, macos-latest}
× python {3.11, 3.12, 3.13, 3.14} running `uv run --python ${{ matrix.python }} pytest -m "not ui and not hardware"`;
`ui-smoke` on ubuntu-latest installing `xvfb` + mesa (`libgl1`, `libglu1-mesa`, `libegl1` etc. as needed by Dear PyGui) and
running `xvfb-run -a uv run pytest -m ui`. Use `uv sync --locked`. `concurrency` cancel-in-progress per ref. Pin actions to
major versions. Validate YAML locally (`uv run python -c "import yaml"` is not available — use `uvx --from yamllint yamllint`
or `uvx actionlint` if available; at least parse it). Update skill tooling file.

### Task 3: Nix flake
`flake.nix` (+ `flake.lock` generated with `nix flake lock`) per plan §8.3 and skill: inputs nixpkgs (nixos-unstable),
pyproject-nix, uv2nix, pyproject-build-systems (follows wired); systems x86_64/aarch64 linux+darwin. Outputs:
`packages.default` (uv2nix virtualenv from `uv.lock` using python313, wrapped with `makeWrapper` so `opencoord` finds libGL,
libX11/libXrandr/libXinerama/libXcursor/libXi/libxkbcommon/wayland on Linux; dearpygui wheel may need `autoPatchelfHook`
or LD_LIBRARY_PATH — do whatever makes it run), `apps.default`, `devShells.default` (uv, python313, ruff, nixfmt-rfc-style,
GL/X libs on LD_LIBRARY_PATH, `UV_PYTHON_DOWNLOADS=never`, `UV_PYTHON`), `checks.<system>.pytest` (runs
`pytest -m "not ui and not hardware"` against the built venv), `formatter`. Install a `.desktop` file
(`packaging/linux/opencoord.desktop`, create it) and udev rule `packaging/linux/99-opencoord-rfexplorer.rules`
(create it: CP210x `10c4:ea60`, `MODE="0660"`, `TAG+="uaccess"`) into `$out/share/applications` and `$out/lib/udev/rules.d`.
Add `.github/workflows/nix.yml` (DeterminateSystems nix-installer-action + magic-nix-cache-action; `nix flake check -L`,
`nix build -L`; ubuntu-latest and macos-latest). Verify locally: `nix build`, `nix flake check`, and
`./result/bin/opencoord --smoke-frames 5` launches. Update skill.

### Task 4: Dockerfile
`Dockerfile` + `.dockerignore` per plan §8.4 and skill. Base `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`. Stages:
`test` (uv sync --locked, ruff, pytest -m "not ui and not hardware"), `build` (uv build → `dist/`; PyInstaller is added in
Task 5 — for now build wheel+sdist only, leave a clearly marked hook comment for Task 5), `artifacts` (FROM scratch, COPY
`dist/`), `runtime` (default/last stage: slim with mesa/X11 libs, installs the built wheel, `ENTRYPOINT ["opencoord"]`).
Add `.github/workflows/docker.yml` (docker/setup-buildx-action, build-push-action with `cache-from/to: type=gha`; PR/push:
build `test` and `runtime` targets without push; tags `v*`: push runtime to `ghcr.io/waayway/opencoord` with
`packages: write`; also run the `artifacts` target with `outputs: type=local,dest=dist`). Verify locally:
`docker build --target test .`, `docker build --target artifacts --output dist/ .`, `docker build -t opencoord:dev .` and
`docker run --rm opencoord:dev --version`. Document run-with-GUI command in README. Update skill.

### Task 5: Native packaging skeleton + build workflow
Per plan §8.2/§8.5 and skill. Create:
- `packaging/icons/opencoord.svg` (simple original icon: stylised spectrum peaks) + `packaging/icons/generate.py`
  (generates `opencoord.png` sizes, `.ico`, `.icns`; may use Pillow via `uv run --with pillow` inline script metadata — do
  not add Pillow to project deps; commit generated outputs).
- `packaging/opencoord.spec` (PyInstaller onedir, name `OpenCoord`, collects dearpygui binaries and package data
  `opencoord/**/*.toml`, icon per OS; macOS `BUNDLE` → `OpenCoord.app` with bundle id `io.github.waayway.opencoord`).
- `packaging/windows/opencoord.iss` (Inno Setup: install to Program Files, Start-menu + optional desktop shortcut,
  uninstaller, `.opencoord` file association, version from `/DAppVersion=`).
- `packaging/macos/make-dmg.sh` (ad-hoc `codesign --force --deep -s -`, `hdiutil create` UDZO DMG with Applications symlink).
- `packaging/linux/make-appimage.sh` (AppDir with AppRun, `.desktop`, icon, `io.github.waayway.opencoord.metainfo.xml`;
  downloads `appimagetool` continuous release; uses `--appimage-extract-and-run`), plus `tar.gz` of the onedir.
- `packaging/build.py` (single entrypoint `uv run python packaging/build.py [--appimage|--installer|--dmg]` that runs
  PyInstaller with the spec then the OS step; prints artifact paths into `dist/`).
- `.github/workflows/build.yml`: triggers pull_request, workflow_dispatch, push tags `v*`; matrix ubuntu-22.04 (x86_64
  AppImage+tar.gz), ubuntu-22.04-arm (aarch64 AppImage+tar.gz), windows-latest (Inno Setup via `choco install innosetup`
  if not present + zip), macos-14 (arm64 DMG). Each job runs the packaged binary with `--smoke-frames 5` (Linux under
  xvfb-run; Windows/macOS directly) then uploads artifacts named `opencoord-<os>-<arch>`.
- Wire the Dockerfile `build` stage hook from Task 4 to also produce the Linux PyInstaller bundle + AppImage into `dist/`.
- Verify locally on Linux: `uv run python packaging/build.py --appimage` produces `dist/OpenCoord-*-x86_64.AppImage`
  which runs with `--smoke-frames 5`. Update skill.

### Task 6: Core types + RF Explorer protocol
Plan §3, skill `device-protocol.md`. TDD.
- `core/types.py`: frozen dataclasses `Sweep(freqs_hz: np.ndarray, dbm: np.ndarray, timestamp: float)` (+ `start_hz`,
  `stop_hz` properties), `DeviceConfig` (all `#C2-F` fields, Hz/kHz converted to int Hz), `ModelInfo` (main_code,
  expansion_code|None, firmware str).
- `device/protocol.py`: command builders (`request_config()`, `set_config(start_hz, stop_hz, top_dbm, bottom_dbm)`,
  `hold()`, `switch_module(main: bool)`, `set_sweep_points(n)` — mark exact formats with `⚠` comments where unverified),
  and an incremental `Parser` (`feed(bytes) -> list[Event]`, events: `ModelReply`, `ConfigReply`, `SweepData(samples dBm
  np.ndarray)`, `Unknown(line)`, `ParseError(reason, data)`) handling `$S`, `$s`, `$z`, `#C2-M`, `#C2-F`, other `#` lines,
  partial frames split across feeds, garbage resync. Combining `SweepData` with the last `ConfigReply` into a `Sweep` is a
  helper `make_sweep(config, samples, timestamp)`.
- **Verify formats against the real device** before finalising: write a throwaway script (not committed) that opens
  `/dev/ttyUSB0` @500000, sends the config request, and captures ~3 s of raw bytes; record them into
  `tests/fixtures/wsub1gplus_config_and_sweeps.bin` + `.json` sidecar (model, firmware, settings). Also consult the official
  spec (search the web for "RF Explorer UART API specification" / the RFExplorer-for-Python source on GitHub) for `$s`/`$z`
  and model codes. Parser tests use the fixture plus synthetic frames. Remove `⚠` from skill items you verified.

### Task 7: Model capability table
`device/models.py` per plan §3 / skill. `Capabilities` dataclass (name, min_hz, max_hz, max_span_hz, is_plus,
expansion: bool, sweep_points_max hint). Table for all known model codes (from the official spec / RFExplorer-for-Python
`RFE_Common` model enum — verify codes, cite source in a comment). `resolve(model: ModelInfo, config: DeviceConfig|None)
-> Capabilities` where config values win; unknown codes → "Unknown model (code N)" using config limits. TDD.

### Task 8: Simulator
`device/simulator.py` per skill: `SimulatedLink` implementing the same public interface the real link will have. Define that
interface now as a `typing.Protocol` `Link` in `device/link_api.py`: `open()`, `close()`, `set_span(start_hz, stop_hz)`,
`hold()`, `switch_module(main: bool)`, `model: ModelInfo|None`, `config: DeviceConfig|None`, `capabilities`,
`sweeps: queue.Queue[Sweep]`, `events: queue.Queue[LinkEvent]` (connected/disconnected/error with user message),
`is_open`. Simulator runs a thread emitting sweeps at ~10 Hz with WSUB1G+ capabilities, seeded RNG, noise floor ≈ −105 dBm,
DVB-T blocks, narrowband carriers, optional intermittent carrier; `set_span` changes the generated range with the same
sweep-point count as the real device would. Deterministic `generate(start_hz, stop_hz, points, t)` function for tests. TDD.

### Task 9: Serial link
`device/link.py`: `SerialLink` implementing `Link`: port discovery (`find_ports()` preferring `10c4:ea60`), open with baud
500000 then 2400 detection, request config, reader thread using `protocol.Parser`, writer command queue, reconnect with
backoff on unplug, error mapping to user messages with OS-specific help (plan §3 Robustness). Unit tests with a fake serial
object (inject a factory). Hardware test (`@pytest.mark.hardware`): open real device, receive model + ≥3 sweeps, set span
470–700 MHz and get sweeps within that range. Run the hardware test locally with `OPENCOORD_HARDWARE=1`.

### Task 10: Debug CLI
`cli.py`: `opencoord-cli [--port P] [--simulator] info` (model, firmware, capabilities, config), `sweep --start MHZ --stop MHZ
[--count N] [--csv FILE]` (prints/writes `MHz,dBm`), `record --raw FILE --seconds S` (raw bytes + `.json` sidecar for
fixtures). Uses `Link` so `--simulator` works. Tests via simulator. Verify against hardware manually and note output in report.

### Task 11: Traces
`core/traces.py` per plan §4 / skill: `TraceSet` (live, max-hold, average over N with running mean, min-hold; reset;
handles axis change by resetting), `noise_floor(dbm)` (robust percentile-based), `find_peaks(freqs, dbm, min_prominence_db,
min_spacing_hz)`, `detected_carriers(trace, floor, threshold_db)`. Pure, numpy, TDD with Hypothesis where useful.

### Task 12: Segmented scanner
`device/scanner.py`: `SegmentedScanner(link, start_hz, stop_hz, resolution: Fast|Normal|Fine)` per skill: segment plan from
capabilities, discard first sweep after reconfig, N sweeps per segment max-held, stitched trace on common axis, progress
callback/queue with partial traces, `estimate_seconds()`, overview (single sweep) mode; driven by a `step()` method callable
from the UI loop or a worker thread (no busy waiting). Test against `SimulatedLink`. Measure on real hardware a Normal
470–960 MHz scan duration and resolution; put measured numbers in the report and tune presets toward ≈60 s (plan Q7).

### Task 13: UI MVP
Per plan §4/Phase 4 and skill `ui.md`: `ui/app.py` layout (toolbar: connect/port selector, preset combo, resolution combo,
start/stop, reset max-hold; spectrum plot over waterfall; right tab bar Device | Scan | Markers | Coordination | Profiles
with placeholders for not-yet tasks; status bar), `ui/spectrum.py` (live/max/avg/min series, hover crosshair readout
MHz/dBm), `ui/waterfall.py` (dynamic texture + LUT, history depth setting), `ui/theme.py`, `ui/shortcuts.py`
(Space, R), `ui/panels/device.py` (port, model/firmware/capabilities, connect/disconnect, error messages), `ui/panels/scan.py`
(start/stop MHz, center/span, built-in presets from plan §4, resolution). `core/settings.py` (platformdirs `settings.toml`,
last port/preset/window size). Uses `Link` (`--simulator` flag selects SimulatedLink), `TraceSet`, `SegmentedScanner`.
UI smoke test runs N frames against the simulator and asserts traces got data. Manually run against hardware; report fps.

### Task 14: Markers, threshold, reference traces
Markers (click-to-place, peak search, next peak left/right, delta marker, table; up to 8; shortcuts M/P/N), draggable
threshold line, up to 4 reference traces (freeze current / load from file later), trace visibility toggles,
auto-scale. Logic (marker math, next-peak search) in a pure `core/markers.py` with TDD; UI in `ui/panels/markers.py` +
`ui/spectrum.py`.

### Task 15: Channel plans, overlays, analysis panel
`coord/channel_plans/eu.toml` + `coord/channel_plans/__init__.py` loader (`load(name)`, `available()` via
`importlib.resources`) per plan §6 (DVB-T 21–48 8 MHz raster from 474 MHz centre of ch21; band annotations with
`pmse = allowed|forbidden|info`). Spectrum overlay: channel grid + numbers, band colouring, per-channel occupancy
(max/avg level, % above threshold — pure function in `core/occupancy.py`, TDD). User exclusion zones (draw/edit/delete,
stored in session state), detected-carrier list panel, amplitude offset setting (per device serial if available else per
model), expansion module switcher when present.

### Task 16: Sessions, exports, importers
`core/session.py` (`.opencoord` zip: `session.json` with `schema_version` + `traces.npz`; contents: settings, traces,
markers, exclusion zones, references, plan placeholder). `io/export_scan.py`: generic CSV (`MHz,dBm` header), WWB-compatible
CSV, WSM-compatible CSV (research the real formats; document sources and uncertainty in the skill — plan deferred Q4),
detected-carriers CSV, PNG of the plot (Dear PyGui `output_frame_buffer` in UI layer; exporter function takes RGBA array —
keep pure part testable; writing PNG may use `zlib`+`struct` stdlib encoder, no Pillow). `io/importers.py`: generic CSV,
RF Explorer for Windows CSV, WWB CSV → `Trace`. File dialogs in UI (Ctrl+S save, Ctrl+O open, Ctrl+E export). Round-trip tests.

### Task 17: Recording, replay, long-run logger
Record all sweeps of a session to a compressed file (`.ocrec`: zip with chunked `.npz` + metadata), replay at 1×/4×/max
through a `ReplayLink` implementing `Link`, long-run logger writing periodic max-hold CSV rows + threshold alert (UI
notification + log line) when any bin exceeds threshold in user-selected ranges. TDD for file format + replay.

### Task 18: Device profiles + spacing configuration
`coord/profiles.py` + `coord/spacing.py` per plan §5.1 and skill `coordination.md`: TOML schema (tuning ranges or channels or
`[[groups]]`, step, kind, spacing preset + overrides), validation with clear errors, load/save in `<config>/profiles/`,
built-in templates (Generic analog mic, Generic IEM, Generic digital, Fixed-channel set) shipped as package TOML; spacing
presets as editable TOML in `<config>/spacing/` seeded from package defaults (`generic-analog`, `conservative`, `iem`,
`digital`; all have 3rd 2Tx/3Tx, 5th 2Tx, 7th 2Tx on, 5th 3Tx off), reset-to-built-in, resolution order run override →
profile override → preset, `SpacingRules.resolve(a, b)` stricter value. Pure parsing/validation separated from file I/O. TDD.

### Task 19: IMD engine
`coord/imd.py` per plan §5.2: vectorised products for 3rd 2Tx, 3rd 3Tx, 5th 2Tx, 7th 2Tx, advanced 5th 3Tx; range-limited;
an incremental `ProductSet` supporting `add(carrier)` / `remove(carrier)` (for backtracking) and
`nearest_conflict(candidate, rules) -> Conflict|None` (rule, required_khz, actual_khz, source carriers). TDD + Hypothesis
(incremental == from-scratch).

### Task 20: Solver
`coord/solver.py` per plan §5.3: `CoordinationRequest` (profiles×quantities, locked carriers, scan trace, exclusions,
channel plan + `allow_forbidden`, threshold_db, guard_hz, prefer_single_group, time_budget_s, run spacing override),
`solve(request) -> Plan` (assignments, unassigned with reasons, backups per profile, warnings, conflict report), `check(plan,
request)`. Most-constrained-first backtracking with time budget and best-partial. Hypothesis invariant: no returned plan
violates any active rule. Performance test: 16 devices in 470–694 MHz, all default orders, < 5 s (mark `slow` if > 1 s).

### Task 21: Profile & spacing editor UI
`ui/panels/profiles.py`: list profiles, new-from-template, edit (name, kind, tuning ranges add/remove, step, paste channel
list — accepts newline/comma/space separated MHz, groups), spacing preset select + per-value overrides, clone, delete,
import/export TOML file; Spacing editor sub-tab: preset CRUD + reset to built-in. Validation errors shown inline.

### Task 22: Coordination UI + plan exports
`ui/panels/coordination.py`: profiles + quantities table, locked carriers entry, options (threshold, guard, allow forbidden,
prefer single group, time budget, run override), Coordinate button running solver in worker thread with progress, results
table (device, freq MHz, scan level, nearest IMD margin, warnings, why-unassigned), frequencies drawn on spectrum, check-mode
for hand-entered plan, save into session. `io/export_plan.py`: CSV, TXT, printable HTML (embedded PNG of spectrum, warnings).
Tests for exporters. End-to-end manual run on hardware: scan → 10 generic devices → coordinate → export; note in report.

### Task 23: Release readiness (no tagging)
`.github/workflows/release.yml` (on `v*` tag: wait for/consume build.yml + docker artifacts via `workflow_call` or by making
build.yml reusable; create GitHub Release with all artifacts + wheel/sdist + `SHA256SUMS`; PyPI step present but disabled
behind `if: false` with comment — plan deferred Q11). Optional `nixosModules.default` (udev rule + group). README full
(features, screenshots placeholders → real screenshot taken with `--simulator` via frame buffer, install per OS incl.
SmartScreen/Gatekeeper notes, supported models with "community-tested" labels, Nix/Docker usage, development),
`CONTRIBUTING.md`, issue templates (bug, device profile, channel plan), `profiles/` community folder with README + one example,
CHANGELOG for 0.1.0. Do not tag or publish.
