# OpenCoord — Design & Implementation Plan

*Date: 2026-10-09 · Status: draft for review · License: GPL-3.0 · Repo (to be created): `github.com/Waayway/opencoord`*

OpenCoord is an open-source, cross-platform spectrum scanner and wireless-microphone / IEM frequency
coordinator for RF Explorer hardware, written in Python with a Dear PyGui interface.

---

## 1. Understanding

### What was asked
- Custom **cross-platform Python app** that talks to the RF Explorer plugged into this machine.
- Show per-frequency what the analyzer sees, with **exports** and everything a normal frequency-scanner app has.
- **Open source**, with a better name than "rf-explorer", hosted as a **GitHub repo created with `gh`**.
- Primary use: **wireless mic / IEM coordination**.
- **Full built-in coordination** (intermod-free frequency sets), with a **simple way to configure many
  amateur / generic devices**.
- **Support all RF Explorer models** (the hardware on hand is a **WSUB1G PLUS SLIM**).
- Region: **Netherlands/EU first**, other regions pluggable.
- UI: **Dear PyGui**. Name: **OpenCoord**. License: **GPL-3.0**. UI language: **English only**.
- Python managed with **uv**; **Nix flake**; **Dockerfile** to build it; native-feeling apps for
  **Windows / macOS / Linux**, all buildable on **GitHub Actions**.
- A **skill folder** in the repo documenting how things are implemented.

### Assumptions (correct me)
- v1 is a single-user desktop app used on a laptop at a venue; no networking / multi-user features.
- "Done" for v1 means you can:
  1. scan 470–960 MHz at fine resolution,
  2. compute an IMD-free plan for N devices that avoids what the scan found,
  3. export scan + plan in formats WWB/WSM and humans can use.
- Python ≥ 3.11 supported; release builds pin 3.13 (dev machine runs 3.14; Dear PyGui 2.3.1 ships cp310–cp314 wheels).

### Environment facts (verified 2026-10-09)
- Device: Silicon Labs **CP2102N** USB-UART (`10c4:ea60`) → `/dev/ttyUSB0`
  (`/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_8e21…-if00-port0`).
- User is in the `uucp` group → serial access without sudo on this (Arch-based) system.
- `gh` is authenticated as **Waayway**; `opencoord` is free on PyPI and under that account.
- Name clash check: "SpectraFox" was rejected (existing SPM microscopy project). No RF project named OpenCoord found.

---

## 2. Architecture

```
opencoord/
  device/
    protocol.py      pure encode/parse of RF Explorer serial protocol (no I/O → unit-testable)
    models.py        capability table for every model (range, max span, RBW, sweep points, expansion modules)
    link.py          serial worker thread: open port, baud detect, autodetect model, stream sweeps, reconnect
    scanner.py       segmented sweeps: split a wide range into narrow spans, stitch into one hi-res trace
    simulator.py     fake device (same interface as link) for tests, CI and offline demo
  core/
    traces.py        live / max-hold / average / min-hold, noise-floor estimate, peak detection
    session.py       scan session (settings, traces, markers, plan) saved as .opencoord (zip: JSON + .npz)
    settings.py      user prefs + paths (platformdirs)
  coord/
    profiles.py      device profiles (tuning ranges, step, fixed channels, IMD spacing); user profiles in TOML
    imd.py           2-tone / 3-tone 3rd- and 5th-order intermod products (numpy-vectorised)
    solver.py        compatible-frequency solver: scan-aware, exclusions, TV channels, locked freqs, backtracking
    channel_plans/   eu.toml (built in); more regions = drop in a TOML file
  io/
    export_scan.py   CSV (generic), WWB-compatible CSV, WSM-compatible CSV, PNG screenshot
    export_plan.py   frequency plan as CSV / TXT / printable HTML
    importers.py     CSV scans from WWB, RF Explorer for Windows, OpenCoord sessions
  ui/
    app.py           Dear PyGui bootstrap, main loop, theme
    spectrum.py      spectrum plot (traces, markers, TV-channel overlay, exclusion zones, threshold line)
    waterfall.py     dynamic-texture waterfall
    panels/          device, scan settings, markers, coordination, profiles editor
tests/               pytest; protocol fixtures recorded from the real WSUB1G+
```

**Data flow.** `link.py` (thread) reads bytes → `protocol.py` parses → `Sweep(freqs, dbm, timestamp)` objects on a
`queue.Queue`. The UI drains the queue once per frame (`set_frame_callback` / render loop) and feeds
`traces.py`; plots are updated from trace arrays. Coordination is a pure function
`solve(profiles, scan, exclusions, plan_rules) -> Plan` run in a worker thread, so the UI never blocks.

**Boundaries.** `protocol.py`, `models.py`, `traces.py` and everything in `coord/` have no I/O and no GUI. They are
fully unit-testable. `simulator.py` lets the whole UI and scanner run in CI without hardware.

---

## 3. Device layer (all RF Explorer models)

Reference: the official *RF Explorer UART API / Interface Specification* and the LGPL
`RFExplorer-for-Python` library (by Ariel Rocholl). LGPL-3 is GPL-3 compatible, but we write a clean, small
protocol module of our own and use the library only as a reference. **All byte formats below must be verified
against the spec and recorded fixtures before relying on them.**

- **Connection:** 8N1, try **500000 baud** first (default on current models), fall back to **2400**.
  Port autodetect matches CP210x VID:PID `10c4:ea60`, with manual port selection as an override.
- **Commands** are framed as `'#'` + length byte + payload. Main ones:
  - `C0`: request config (model + current config)
  - `C2-F:SSSSSSS,EEEEEEE,TTTT,BBBB`: set start/end kHz and amplitude top/bottom
  - `CH`: hold
  - `CM`: switch main/expansion module
  - sweep-points commands for large sweeps (`CJ`/`Cj`, verify)
- **Responses:**
  - `#C2-M:` model / expansion / firmware
  - `#C2-F:` start, step, amp range, sweep steps, active module, mode, min/max freq, max span, RBW, amp offset
  - Sweeps arrive as `$S` (8-bit count), `$s` (extended), `$z` (16-bit count). Each sample is `-byte/2` dBm.
- **`models.py`:** a table keyed by model code with name, frequency range, max span, typical sweep-point limits and
  whether it is a "Plus" model. These values are only **hints**: the device-reported `#C2-F` min/max/span always wins.
  Covers 433M, 868M, 915M, WSUB1G, WSUB1G+, 2.4G, WSUB3G, 6G, ISM/6G combos, 6G+, 4G+, 2400+ and unknown codes
  (degrade gracefully: show the code and use the reported limits).
- **Expansion modules:** a module switcher in the UI when an expansion model is reported.
- **Segmented scanning (`scanner.py`):** RBW grows with span, so a 470–960 MHz single sweep is too coarse to see
  individual mics. The scanner:
  1. splits the range into segments (e.g. 10–20 MHz, configurable "resolution" preset),
  2. sweeps each segment N times,
  3. stitches the results into one trace with max-hold per segment.
  
  It shows progress and keeps a "quick overview" mode (single wide sweep) as well.
- **Robustness:**
  - handle unplugging and replugging, with auto-reconnect
  - resync on garbage bytes (scan for the next `$`/`#` marker)
  - timeouts
  - a clear error when the port is busy (e.g. RF Explorer for Windows is open) or permission is denied, with OS-specific
    help (Linux `uucp`/`dialout` group; Windows/macOS CP210x driver link)
- **Amplitude offset:** a user-set dB offset for external attenuators, preamps or antennas, stored per device serial.

---

## 4. Scanner features (the "normal frequency scanner" set)

| Area | Features |
|---|---|
| Tuning | start/stop, center/span, presets, custom presets saved by user, step/RBW readout |
| Resolution (Q7) | scan-resolution presets **Fast / Normal / Fine** (segment size + sweeps per segment); estimated scan time shown before starting; aim for a Normal 470–960 MHz scan in about 60 s, with the final defaults tuned on real hardware in Phase 3 |
| Presets | Full UHF 470–960, TV 21–48 (470–694), 694–790, 823–832, 863–865, 1785–1805 *(only on models that reach it)*, ISM 433 / 868, VHF 174–216, Overview (device full range) |
| Traces | live, max-hold, average (N sweeps), min-hold; toggle visibility; reset; up to 4 **reference traces** (freeze / load a previous scan to compare) |
| Markers | click-to-place, peak search, next peak left/right, delta marker, marker table with freq/level, up to 8 markers |
| Display | dBm Y-axis with adjustable range, hover crosshair readout (MHz / dBm / TV channel), threshold line, auto-scale |
| Waterfall | scrolling history (configurable depth), colormap + min/max level, pause; time axis |
| Overlays | TV channel grid with numbers (from channel plan), per-channel occupancy indicator (max / avg level, % above threshold), user exclusion zones (drag to draw), coordinated frequencies |
| Analysis | noise-floor estimate, list of detected carriers (peaks above floor + X dB), channel power for a selected span |
| Recording | record a session over time (all sweeps, compressed), replay it, long-run logging to CSV with optional threshold alert |
| Files | save/open session (`.opencoord`), import scans (CSV from WWB / RF Explorer for Windows / generic) |
| Export | scan CSV (generic, `MHz,dBm`), WWB-compatible CSV, WSM-compatible CSV, PNG screenshot of plot, detected-carrier list CSV |
| UX | dark theme, keyboard shortcuts (space = start/stop, M = marker, P = peak, R = reset max-hold…), status bar (device, firmware, RBW, sweeps/s, port) |

---

## 5. Coordination

### 5.1 Device profiles (the "simple way to configure amateur devices")
A profile describes **one device type**, and a coordination contains *quantities* of profiles.
Profiles are TOML in the user config dir and can be edited in the app via a **profile editor**.

```toml
[profile]
name = "Generic UHF handheld 823-832"
kind = "mic"                 # mic | iem | other
tuning = [[823.0, 832.0]]    # one or more MHz ranges
step_khz = 25                # tuning step
channels = []                # optional fixed channel list (MHz); overrides tuning+step when present
spacing = "generic-analog"   # preset name, or an inline [spacing] table
```

- **Groups/banks (decided, Q6):** a fixed channel list can be flat, or grouped into banks, e.g.
  `[[groups]] name = "A" channels = [...]`. When groups are present, the solver can prefer putting all devices of
  one profile in a **single group**, which is what cheap sets need. This is a toggle, on by default.
- **Editor workflow:** "New profile" → pick a **template** (Generic analog mic, Generic IEM, Generic digital,
  Fixed-channel cheap set) → type tuning range(s) and step **or paste a channel list** → save.
  You can also clone or export/import profiles as files, which makes them easy to share.
- **Spacing is fully configurable** (decided 2026-10-09). There are no hard-coded values anywhere in the solver;
  everything comes from data, at three levels:
  1. **Spacing presets** are editable TOML files in `<config>/spacing/`. The app seeds them on first run:
     *generic-analog*, *conservative*, *iem* and *digital*. Users can edit, clone, rename, delete, import and export
     presets in a **Spacing editor** panel. "Reset to built-in" restores the shipped values.
  2. **Per profile:** pick a preset, then optionally override individual values (carrier, 3rd-order 2Tx, 3rd-order
     3Tx, 5th-order 2Tx, 7th-order 2Tx, advanced 5th-order 3Tx). Each value can be in kHz or 0 for disabled.
  3. **Per coordination run:** an optional global override (e.g. "tight mode" scaling all spacings by a factor, or
     forcing 5th order on).
- **Resolution order:** run override → profile override → profile's preset. For a pair of different profiles the
  **stricter** value of the two applies.
- The results view shows **which rule and which value** blocked a candidate, so users can tune spacing from
  experience.

### 5.2 IMD engine (`imd.py`)
**Default orders (decided 2026-10-09): 3rd + 5th + 7th**, on in every seeded preset. Each order can still be
disabled or re-spaced per preset, profile or run (§5.1).

| Order | Products | Default |
|---|---|---|
| 3rd, 2-Tx | `2f1 − f2` | on |
| 3rd, 3-Tx | `f1 + f2 − f3` | on |
| 5th, 2-Tx | `3f1 − 2f2` | on |
| 7th, 2-Tx | `4f1 − 3f2` | on |
| 5th, 3-Tx (`2f1 + f2 − 2f3`, …) | | off (advanced) |

- Computed with vectorised numpy. Only products that land within the union of the profile tuning ranges, plus a
  margin, are kept.
- The 3-Tx set grows as O(n³), so products are maintained **incrementally** as the solver places each carrier.
  They are never recomputed from scratch.
- Performance target (Phase 7): 16 devices in 470–694 MHz with all default orders in under 5 s, and 40 devices
  within the time budget. The solver returns the best partial result if the budget runs out.

### 5.3 Solver (`solver.py`)
1. **Candidate grid** per profile: tuning ranges × step (or fixed channels).
2. **Remove:**
   - excluded TV channels and user exclusion zones
   - frequencies where the scan's max-hold > noise floor + *threshold dB* within ± guard bandwidth
3. **Rank** candidates by scan level (quietest first).
4. **Assign devices** most-constrained-first (fewest candidates) with backtracking, checking carrier spacing and each
   IMD order against all already-placed carriers *and* their products. Run with a time budget.
5. **Output:**
   - the plan (device → frequency, group/bank if fixed-channel), unassigned devices with reasons,
   - **backup frequencies** per profile,
   - a compatibility report.
- **Locked frequencies:** devices already tuned (or foreign transmitters you can't change) are entered as fixed
  carriers and participate in IMD checks.
- **Check mode:** validate a hand-made plan and highlight conflicts.

### 5.4 Coordination UI
Profiles + quantities table → "Coordinate" → result table (device, freq, scan level, nearest IMD hit)
→ frequencies drawn on the spectrum. Then export the plan as CSV, TXT or a printable HTML sheet with the spectrum
image, and save it into the session.

---

## 6. Channel plans

`coord/channel_plans/eu.toml`:
- DVB-T 8 MHz grid, channels 21–48 (470–694 MHz)
- the 694–790 MHz LTE/700 band marked "not for PMSE in NL"
- 823–832 MHz (mic duplex gap)
- 863–865 MHz (licence-free)
- 1785–1805 MHz, marked as informational

Plans are pure data (name, raster, channel number ↔ freq, band annotations with a "legal for PMSE" flag), so US/UK/etc.
are additional files.

**Legality handling (decided, Q3):**
- Bands are coloured on the spectrum as allowed / forbidden / info.
- The solver **skips "forbidden" bands by default**. A per-run "allow forbidden bands" toggle lets you opt in, and
  any plan that uses them carries a visible warning in the UI and in every export.
- The app never hard-blocks.
- Band annotations in `eu.toml` are best-effort and should be checked against the current Agentschap Telecom /
  RDI rules before v0.1.

---

## 7. Testing strategy
- **TDD** for `protocol.py`, `models.py`, `traces.py`, `scanner.py` (stitching), `imd.py`, `solver.py`, exporters/importers.
- **Recorded fixtures:** capture raw byte streams from the WSUB1G+ (config reply, sweeps of each `$S/$s/$z` kind if
  available) into `tests/fixtures/` and replay them through the parser.
- **Simulator** generates synthetic spectra (noise floor + carriers + TV DVB-T blocks) for scanner/UI tests.
- **Solver tests:** known small cases with hand-verified results, property tests (Hypothesis) asserting no returned
  plan violates any spacing rule.
- **UI:** smoke test that starts the app against the simulator headless-ish (Dear PyGui viewport hidden) for a few frames.
- **Hardware smoke test** (manual, marked `@pytest.mark.hardware`): connect, read model, one sweep.

---

## 8. Tooling, packaging & distribution

### 8.1 Python tooling: uv
- **uv manages everything:**
  - Python version via `.python-version`; release builds pin **3.13**, CI also tests 3.11–3.14
  - the venv, dependencies and `uv.lock` (committed)
  - running (`uv run opencoord`) and building (`uv build`)
- `pyproject.toml` with the `uv_build` (or hatchling) backend; entry points `opencoord` (GUI) and `opencoord-cli`.
  `python -m opencoord` also works.
- Runtime deps: `dearpygui`, `pyserial`, `numpy`, `platformdirs`, `tomli-w`.
  Dev group: `pytest`, `hypothesis`, `ruff`, `mypy`, `pyinstaller`.
- Common commands:
  - `uv sync`: set up
  - `uv run pytest`
  - `uv run ruff check`
  - `uv run opencoord --simulator`: run without hardware

### 8.2 Native, functional apps per OS (PyInstaller + OS packager)
PyInstaller `--onedir` from one shared `packaging/opencoord.spec` (bundles channel plans, profile templates, icons),
then wrapped per OS so it installs and launches like a normal app:

| OS | Artifact | How |
|---|---|---|
| **Windows** (x64) | `OpenCoord-<ver>-win64-setup.exe` + portable `.zip` | Inno Setup script `packaging/windows/opencoord.iss`: Start-menu shortcut, uninstaller, `.opencoord` file association, link to the CP210x driver |
| **macOS** (arm64, + x86_64 if a runner is available) | `OpenCoord-<ver>-macos-<arch>.dmg` containing `OpenCoord.app` | PyInstaller `BUNDLE` with `Info.plist` + `.icns`; `hdiutil` DMG; ad-hoc signed (notarisation optional, needs an Apple Developer ID; see open questions) |
| **Linux** (x86_64, + aarch64) | `OpenCoord-<ver>-x86_64.AppImage` + `.tar.gz` | AppDir with `.desktop` file, icon, AppStream metadata, built with `appimagetool`; built on the oldest supported glibc (ubuntu-22.04 runner) for broad compatibility; ships `99-opencoord-rfexplorer.rules` udev rule + install hint |
| **NixOS / Nix** | `nix run github:Waayway/opencoord` | flake (§8.3) |
| **Any OS with Python** | wheel / sdist | `uv build` → GitHub release (PyPI optional) |

App icon from one SVG source → `.ico` / `.icns` / `.png` sizes generated by a script in `packaging/icons/`.

### 8.3 Nix flake (`flake.nix`)
- Inputs: `nixpkgs`, `pyproject-nix`, `uv2nix`, `pyproject-build-systems`, so the Nix build uses the **same `uv.lock`**.
- Outputs (for `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`, `aarch64-darwin`):
  - `packages.default` / `packages.opencoord`: virtualenv built from `uv.lock` and wrapped with `makeWrapper` so
    Dear PyGui finds `libGL`, X11/Wayland libs. Ships the `.desktop` file, icon and udev rule.
  - `apps.default`: `nix run` launches the GUI
  - `devShells.default`: `uv`, Python 3.13, `ruff`, plus GL/X libs on `LD_LIBRARY_PATH` so `uv run opencoord` works in
    the shell. `UV_PYTHON_DOWNLOADS=never` makes it use Nix's Python.
  - `checks`: pytest run inside the Nix build
  - `formatter`: `nixfmt`
- Optional `nixosModules.default` adds the udev rule and group (nice-to-have, Phase 9).

### 8.4 Docker (`Dockerfile`)
Multi-stage, built on `ghcr.io/astral-sh/uv` + Debian bookworm (glibc 2.36):
1. **`test` stage:** `uv sync --locked`, ruff, pytest (simulator only).
2. **`build` stage:** `uv build` (wheel/sdist) + PyInstaller Linux bundle + AppImage.
3. **`artifacts` stage** (`FROM scratch`): extracts the outputs with
   `docker build --target artifacts --output dist/ .`, so a Linux release can be reproduced on any machine with Docker.
4. **`runtime` stage** (default): slim image with the app plus mesa/X11 libs. It can run the GUI with
   `docker run --device /dev/ttyUSB0 -e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix ghcr.io/waayway/opencoord`
   (documented, best-effort; Wayland via XWayland).

A `.dockerignore` keeps the context small. The runtime image is published to **GHCR** on release.

### 8.5 GitHub Actions (`.github/workflows/`)
| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | push / PR | `astral-sh/setup-uv` → ruff, mypy, pytest on ubuntu / windows / macos × py 3.11–3.14; UI smoke test on Linux under `xvfb-run` |
| `build.yml` | PR (artifact upload only), `workflow_dispatch`, tags `v*` | matrix: `ubuntu-22.04` (AppImage + tar.gz), `ubuntu-22.04-arm` (aarch64 AppImage), `windows-latest` (Inno Setup installer + zip), `macos-14` (arm64 dmg), Intel macOS runner if still offered → artifacts uploaded per run |
| `nix.yml` | push / PR | install Nix (DeterminateSystems action) → `nix flake check`, `nix build` on Linux + macOS; optional Cachix/magic-nix-cache |
| `docker.yml` | push / PR (build + test stage), tags (push to GHCR) | `docker/build-push-action` with GHA cache; also runs the `artifacts` target |
| `release.yml` | tag `v*` | waits on build/docker, creates the GitHub Release with all installers, AppImages, DMGs, wheel, sdist and `SHA256SUMS`; optional PyPI publish via trusted publishing |

Every artifact users install can be built from a clean GitHub runner. No local-only steps.

### 8.6 Repo files
- `LICENSE` (GPL-3.0), `README.md` (screenshots, install per OS, supported models, serial permissions)
- `CONTRIBUTING.md`, `CHANGELOG.md`
- issue templates (bug / new device profile / new channel plan)
- a `profiles/` folder of community device profiles
- `.claude/skills/` (§8.7)

### 8.7 Implementation skill (`.claude/skills/opencoord-implementation/`)
A Claude Code skill that documents **how things are implemented** in this repo: architecture and layering rules,
the device protocol, coordination internals, tooling/packaging, and conventions. It is created now with the decided
design and **must be updated in the same commit whenever an implementation detail changes** (part of each phase's
definition of done).

### 8.8 Language
The UI is **English only**. Strings are not wrapped for i18n (YAGNI).

---

## 9. Implementation phases

Each phase ends with passing tests, an updated implementation skill, and a commit. Phases 1–4 give a usable scanner, 7–8 add coordination.

### Phase 0: Repo bootstrap
- [x] `git init`, implementation skill folder (`.claude/skills/opencoord-implementation/`)
- [ ] `uv init --package`, `.python-version`, `pyproject.toml`, `uv.lock`, package skeleton, `LICENSE` (GPL-3.0), README stub
- [ ] ruff/mypy/pytest config, `ci.yml`
- [ ] `flake.nix` (devShell + package + app + checks) and `nix.yml`
- [ ] `Dockerfile` (test/build/artifacts/runtime stages), `.dockerignore`, `docker.yml`
- [ ] Packaging skeleton: `packaging/opencoord.spec`, placeholder icon, `build.yml` producing a "hello window" app
  on all OSes. This proves the distribution pipeline early, before features exist.
- [ ] `gh repo create Waayway/opencoord --public --source . --push` *(after plan approval)*
- **Done when:** CI, Nix, Docker and build workflows are green and every OS artifact launches a Dear PyGui window.

### Phase 1: Protocol & models (no hardware)
- [ ] `protocol.py`: command builders + streaming parser (`$S`, `$s`, `$z`, `#C2-M`, `#C2-F`, unknown lines), with resync
- [ ] `models.py`: full model table + capability resolution from config reply
- [ ] `simulator.py` exposing the same interface as `link.py`
- **Done when:** parser round-trips all fixture types; the simulator produces sweeps.

### Phase 2: Serial link & real device
- [ ] `link.py`: port discovery (VID:PID), baud detect, config request, sweep stream thread, reconnect, error mapping
- [ ] Record real fixtures from the WSUB1G+ Slim; hardware smoke test
- [ ] Tiny CLI `opencoord-cli info|sweep --start --stop --csv` for debugging
- **Done when:** the CLI prints model/firmware and a valid sweep from `/dev/ttyUSB0`.

### Phase 3: Traces & scanner
- [ ] `traces.py` (live/max/avg/min, noise floor, peaks); `scanner.py` segmented scan + stitching + progress
- **Done when:** a stitched 470–960 MHz scan (simulator and real) has the expected resolution and its tests pass.

### Phase 4: UI MVP
- [ ] Dear PyGui app: device panel, tuning/presets, spectrum with traces, waterfall texture, hover readout, status bar
- [ ] Start/stop, max-hold reset, dark theme, keyboard shortcuts, settings persistence
- **Done when:** live scanning works smoothly (target ≥ 30 fps UI) on the real device.

### Phase 5: Scanner completeness
- [ ] Markers (peak / next / delta / table), threshold line, reference traces
- [ ] EU channel plan overlay with occupancy, exclusion zones, detected-carrier list, amplitude offset, module switcher
- **Done when:** every row of the §4 table except Files/Export/Recording is implemented.

### Phase 6: Files, exports, recording
- [ ] Session save/open, scan CSV / WWB CSV / WSM CSV / PNG export, importers, record & replay, long-run logger
- **Done when:** an exported scan round-trips and imports into WWB and WSM (verified manually).

### Phase 7: Coordination engine
- [ ] `profiles.py` + TOML schema + templates; `spacing.py` with editable preset files, seeding, and override resolution
- [ ] `imd.py`, `solver.py` (candidates, scan-aware filtering, backtracking, locked carriers, check mode, backups)
- **Done when:** property tests show no plan violates its rules, and a 16-device plan in 470–694 MHz solves in under 5 s.

### Phase 8: Coordination UI
- [ ] Profile editor (templates, paste channel list, clone/import/export) and Spacing editor (presets CRUD, reset to built-in)
- [ ] Coordination panel, results on spectrum, plan exports (CSV/TXT/HTML)
- **Done when:** the end-to-end flow (scan → add 10 amateur devices → coordinate → export) works on the real device.

### Phase 9: Release v0.1.0
- [ ] Polish installers: Inno Setup (shortcuts, file association), DMG layout, AppImage desktop integration, udev rule
- [ ] `release.yml` (GitHub Release + `SHA256SUMS`), GHCR image, optional NixOS module, optional PyPI
- [ ] README with screenshots and per-OS install instructions, `CHANGELOG.md`, tag `v0.1.0`
- **Done when:** a fresh Windows, macOS and Linux machine can install from the release page and scan with an RF Explorer.

---

## 10. Out of scope for v1 (YAGNI)
Network/remote viewing, multi-device simultaneous scanning, RF Explorer signal-generator control, direct control of
wireless receivers (Shure/Sennheiser network protocols), mobile apps, manufacturer compatibility databases.

---

## 11. Decisions log & deferred questions

### Decided
| # | Topic | Decision |
|---|---|---|
| 1 | Spacing values | Fully configurable: editable presets + per-profile + per-run overrides (§5.1) |
| 2 | IMD orders | 3rd (2Tx, 3Tx) + 5th + 7th on by default, all configurable (§5.2) |
| 3 | Band legality | Annotate + warn; solver skips forbidden bands unless opted in; never hard-block (§6) |
| 5 | Show size | Target 16 devices in under 5 s, 40 devices within the time budget (§5.2) |
| 6 | Fixed-channel devices | Optional groups/banks in profiles, "keep a profile in one group" preference (§5.1) |
| 9 | Other RF Explorer models | WSUB1G+ is hardware-tested; other models are spec/fixture-tested and labelled "community-tested" in the README until someone verifies them |
| 10 | Repo | `Waayway/opencoord`, **public** from the start |
| 13 | Language | English only (§8.8) |
| 15 | Docker | Primarily for **building** (test/build/artifacts stages); the GUI runtime image is best-effort and documented as such |

### Deferred until the app is built
Each item is decided at the phase where it can be judged with a working build:

| # | Question | Decide in | Default until then |
|---|---|---|---|
| 4 | Exact WWB / Sennheiser WSM CSV import formats | Phase 6 (verify by importing real exports) | Implement both from public format notes; keep the generic `MHz,dBm` CSV |
| 7 | Final scan resolution / time defaults | Phase 3 (measure on WSUB1G+) | **Presets set from measured WSUB1G+ sweep rates (Task 12):** Fast 20 MHz × 1 @ 112 pts (180 kHz bins, ~15 s), Normal 40 MHz × 3 @ 512 pts (78 kHz bins, RBW 95 kHz, ~58 s est.), Fine 20 MHz × 2 @ 512 pts (39 kHz bins, ~2 min). End-to-end timing of a real Normal 470–960 MHz scan still to confirm (device dropped off USB during Task 12) before moving this to Decided |
| 8 | macOS signing / notarisation; Intel-Mac build | Phase 9 | Ad-hoc signed arm64 DMG; Intel only if a GitHub runner is available |
| 11 | Publish to PyPI | Phase 9 | GitHub Releases only; the `uv build` wheel is attached to the release |
| 12 | Importing existing scans as fixtures | Phase 6 | Generic CSV + RF Explorer for Windows CSV importers; add user files as fixtures when provided |
| 14 | Windows code signing (SmartScreen) | Phase 9 | Unsigned v0.1 with a README note; consider SignPath OSS later |
| 16 | Extra Linux formats (Flatpak, .deb/.rpm) | Phase 9 | AppImage + tar.gz + Nix + Docker |
| 17 | Per-OS build details (installer UX, file associations, udev install flow) | Phase 0 skeleton, finalised in Phase 9 | As described in §8.2 |
