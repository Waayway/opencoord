# UI (Dear PyGui)

- **Dear PyGui 2.x.** One viewport. Logic and rendering are split:
  - `ui/controller.py` `Controller` + `ui/state.py` `AppState` hold all behaviour and state and never import
    Dear PyGui (see `architecture.md`, "UI controller"); they are unit-tested against `SimulatedLink`.
  - The DPG modules only render `controller.state` and call controller intents.
- **Layout** (`ui/app.py` `App`):
  - toolbar (`toolbar.*`): port combo + Connect/Disconnect, Live/Scan radio, preset combo, resolution combo,
    Start/Stop, Reset max hold
  - spectrum plot over the waterfall, in one `subplots(2, 1, link_all_x=True)` so the MHz axes line up
  - right-hand tab bar (`tabs`, width 370): Device | Scan | Markers | Coordination | Profiles; the last three
    show "Coming soon" until their tasks land
  - a status bar (`status.line`): connection, mode, range, step/RBW, sweeps/s, last message, fps
- **Shared widget values:** the toolbar and the panels show the same port/preset/resolution via value-registry
  `source` items (`ui.port_choice`, `ui.preset`, `ui.resolution`). Radio buttons do **not** redraw when their
  `source` changes, so both mode radios (`toolbar.mode`, `scan.mode`) are set by tag.
- **Render loop:** manual. `App.frame()` = `controller.tick()` → each view's `update(state)` → `render_dearpygui_frame()`.
  `App.build()` / `frame()` / `close()` are separate so tests can drive frames; `App.run(max_frames)` wraps them
  and logs frames, fps and per-frame work (tick + view updates, excluding render). Ctrl+C exits normally.
- **Change tracking:** views compare `state.ui_version` (widgets), `state.trace_version` (series) and
  `WaterfallHistory.version` with what they last pushed, so `set_value` with lists happens once per new data,
  never as a per-frame loop over points.
- **Spectrum** (`spectrum.py` `SpectrumView`):
  - one `line_series` per trace, tags `spectrum.trace.{live,avg,min,max,scan}` (scan = the partial stitched
    trace while a scan runs); `set_value([mhz.tolist(), dbm.tolist()])`; toggle visibility from the legend
  - X axis MHz, Y axis dBm (initial -120..-20). Axis limits set with `set_axis_limits` are locked, so they are
    released with `set_axis_limits_auto` 3 frames later (they must render once first); a new `view_range_hz`
    re-applies them to both x axes, so the user can still zoom and pan
  - hover: plot `crosshairs=True`; `spectrum.readout` shows cursor MHz/dBm plus the nearest level of max hold
    (or scan / live) via the pure `readout()`
  - (planned, Phase 5) TV channel overlay as shaded areas, markers as `drag_line` + `annotation`, threshold line
- **Waterfall** (`waterfall.py` `WaterfallView`): a `dynamic_texture` `DISPLAY_BINS` (1024) wide and `depth` rows
  high (settings `waterfall_depth`, default 300), shown with an `image_series` whose bounds are the history's
  MHz range. Rows come from `WaterfallHistory` (newest first, resampled with `resample_max`: max per column so
  a one-bin carrier survives downsampling a 6k-point scan, interpolation when upsampling, NaN outside the data).
  Colour: precomputed 256-entry viridis LUT (`build_lut`) + background for NaN, fixed -115..-35 dBm (adjustable in
  Phase 5). Only the newly pushed rows are colour-mapped (the RGBA buffer is shifted); a range or depth change
  remaps everything (about 2.5 ms for 300×1024) and a depth change recreates the texture. The numpy RGBA buffer
  is passed to `set_value` directly (buffer protocol). The y axis shows the filled rows, at least
  `MIN_VISIBLE_ROWS` = 20, so a scan's single row is visible.
- **Panels:** `panels/device.py` `DevicePanel` (port combo with "Auto-detect" + `find_ports()` results, manual port
  entry, Refresh, Connect/Disconnect, "Connect on start" = `auto_connect`, model/firmware/range/max span/points/
  tuned span/step/RBW/expansion, last error in red). `panels/scan.py` `ScanPanel` (mode, preset, start/stop and
  center/span in MHz applied on Enter, resolution with bin width, estimated scan time, progress bar with
  segment i/N, Start/Stop, Reset max hold, waterfall rows, shortcut help).
- **Item tags:** string tags namespaced per panel (`"spectrum.trace.max"`, `"device.connect"`, `"scan.start"`).
- **Theme** (`theme.py`): dark global theme; Okabe-Ito trace colours (live sky blue, max orange, average green,
  min purple, scan yellow) via per-series themes (`series_theme(key)`).
- **Shortcuts** (`shortcuts.py`): `SHORTCUTS` maps Space → `Controller.toggle` (start/stop the selected mode) and
  R → `Controller.reset_max_hold`; they are ignored while one of the panels' text/number inputs is active.
  (planned) M marker, P peak, N next peak, Ctrl+S save session, Ctrl+E export.
- **Simulator:** `opencoord --simulator` uses `SimulatedLink(sweep_points=512)` (like the WSUB1G+ at Normal/Fine) and
  connects on start; Live over 470-960 MHz is clamped to the 342.37 MHz max span at 512 points.
- **Language:** English strings inline; no i18n. Labels use ASCII hyphens (default font).
- **Testing:** `tests/ui/test_controller.py` and `test_state.py`/`test_views.py` need no display.
  `tests/ui/test_smoke.py` (`@pytest.mark.ui`, skipped without `DISPLAY`/`WAYLAND_DISPLAY`) runs
  `python -m opencoord --smoke-frames 5` in a subprocess and, in-process, drives `App` frames against a
  512-point simulator: Live for 60 frames (asserts trace + waterfall + series data), then a Fast scan of
  470-500 MHz to completion; it logs fps. Dear PyGui segfaults when a second viewport/context is created in the
  same process, so only one test may open a window in-process.
- **Performance (2026-10-09, 144 Hz Wayland desktop, vsync on):** Live at 512 points ~144 fps (vsync-bound),
  per-frame work ~0.2-0.4 ms; a 6261-point Normal 470-960 MHz scan trace re-pushed every frame still 144 fps.
  Spikes up to ~10 ms only on the first frame and on range changes (full waterfall remap + texture upload).
