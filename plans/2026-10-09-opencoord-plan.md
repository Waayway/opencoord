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
- UI: **Dear PyGui**. Name: **OpenCoord**. License: **GPL-3.0**.

### Assumptions (correct me)
- v1 is a single-user desktop app used on a laptop at a venue; no networking / multi-user features.
- "Done" for v1 means you can:
  1. scan 470–960 MHz at fine resolution,
  2. compute an IMD-free plan for N devices that avoids what the scan found,
  3. export scan + plan in formats WWB/WSM and humans can use.
- Python ≥ 3.11 supported (dev machine runs 3.14; Dear PyGui 2.3.1 ships cp310–cp314 wheels).

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

- **Editor workflow:** "New profile" → pick a **template** (Generic analog mic, Generic IEM, Generic digital,
  Fixed-channel cheap set) → type tuning range(s) and step **or paste a channel list** → save.
  You can also clone or export/import profiles as files, which makes them easy to share.
- **Spacing presets:**
  - *Generic analog*: 350 kHz carrier spacing, 3rd-order 2Tx 100 kHz, 3Tx 50 kHz, 5th-order off
  - *Conservative*: wider spacings, 5th-order on
  - *IEM*: wider carrier spacing, stricter IMD
  - *Digital*: tighter spacings
  - *Custom*

  These defaults must be reviewed; see open questions.

### 5.2 IMD engine (`imd.py`)
For a candidate set *F*: 2-tone 3rd order `2f1−f2`; 3-tone 3rd order `f1+f2−f3`; optional 2-tone 5th order
`3f1−2f2` (and 7th as an advanced option). Vectorised with numpy; products only computed within the union of
profile tuning ranges (+ margin).

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
are additional files. **NL legality details are to be confirmed by the user** (see open questions); the app only
*annotates* and never enforces.

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

## 8. Packaging, CI, repo
- `pyproject.toml` (hatchling), managed with **uv**; entry point `opencoord`; `python -m opencoord`.
- Dependencies:
  - `dearpygui`, `pyserial`, `numpy`, `platformdirs`, `tomli-w`
  - dev: `pytest`, `hypothesis`, `ruff`, `mypy`
- GitHub Actions:
  - lint + tests on Linux/Windows/macOS × Python 3.11–3.14
  - release workflow builds **PyInstaller** single-folder apps for all three OSes on tag
- Repo files:
  - `LICENSE` (GPL-3.0), `README.md` (screenshots, install, supported models, Linux serial permissions)
  - `CONTRIBUTING.md`, issue templates (bug / new device profile / new channel plan)
  - a `profiles/` community folder for shared device profiles
- Linux: document group membership and ship an optional udev rule.

---

## 9. Implementation phases

Each phase ends with passing tests and a commit; phases 1–4 give a usable scanner, 7–8 add coordination.

### Phase 0: Repo bootstrap
- [ ] `git init`, `.gitignore`, `pyproject.toml`, package skeleton, `LICENSE` (GPL-3.0), README stub
- [ ] ruff/mypy/pytest config, GitHub Actions CI
- [ ] `gh repo create Waayway/opencoord --public --source . --push` *(after plan approval)*

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
- [ ] `profiles.py` + TOML schema + templates + spacing presets
- [ ] `imd.py`, `solver.py` (candidates, scan-aware filtering, backtracking, locked carriers, check mode, backups)
- **Done when:** property tests show no plan violates its rules, and a 16-device plan in 470–694 MHz solves in under 5 s.

### Phase 8: Coordination UI
- [ ] Profile editor (templates, paste channel list, clone/import/export)
- [ ] Coordination panel, results on spectrum, plan exports (CSV/TXT/HTML)
- **Done when:** the end-to-end flow (scan → add 10 amateur devices → coordinate → export) works on the real device.

### Phase 9: Release v0.1.0
- [ ] PyInstaller builds via Actions, README with screenshots, tag `v0.1.0`, GitHub release

---

## 10. Out of scope for v1 (YAGNI)
Network/remote viewing, multi-device simultaneous scanning, RF Explorer signal-generator control, direct control of
wireless receivers (Shure/Sennheiser network protocols), mobile apps, manufacturer compatibility databases.

---

## 11. Open questions

1. **Spacing defaults:** what spacings do your amateur devices actually tolerate? The presets in §5.1 are typical
   generic values. Do you have measured figures, or a preferred source (e.g. a WWB "generic" profile)?
2. **IMD orders:** is 3rd-order (2Tx + 3Tx) enough by default, with 5th as an option? Or should 5th be on by default
   for IEMs?
3. **NL / EU band legality:**
   - Which bands do you actually use?
   - Should 694–790 MHz and 1785–1805 MHz be annotated as allowed or forbidden for your use?
   - Should the app warn when a plan lands in a non-PMSE band?
4. **WWB / WSM import formats:** do you have WWB and/or Sennheiser WSM installed, to verify the exact CSV formats
   they import? Which one matters more?
5. **Typical show size:** how many channels do you usually coordinate (10? 40?) This sets the solver's performance
   target.
6. **Fixed-channel devices:** do many of your amateur sets only offer a fixed channel list (groups/banks)? Should
   OpenCoord model groups/banks explicitly, or is a flat channel list enough?
7. **Scan resolution vs. time:** how long is acceptable for a full 470–960 MHz hi-res scan at a venue (30 s?
   2 min?)? This picks the default segment size.
8. **Minimum Python / platforms:** is 3.11+ fine? Do you need macOS builds (needs a Mac for testing / signing), or
   are Linux + Windows enough for v1?
9. **Other hardware:** do you have (or can you borrow) other RF Explorer models to test "all models" support? If
   not, non-WSUB1G+ models will be fixture/spec-tested only, and labelled "community-tested" in the README.
10. **Repo visibility & ownership:** create `Waayway/opencoord` as **public** immediately, or private until v0.1?
    Should a GitHub org be created instead?
11. **PyPI:** publish to PyPI (`pip install opencoord`) at v0.1, or only GitHub release binaries?
12. **Existing data:** do you have old scans (RF Explorer for Windows CSVs, WWB scans) we should support importing
    and use as test fixtures?
13. **Language:** UI in English only, or also Dutch?
