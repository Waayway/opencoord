# UI (Dear PyGui)

- **Dear PyGui 2.x.** One viewport. Logic and rendering are split:
  - `ui/controller.py` `Controller` + `ui/state.py` `AppState` hold all behaviour and state and never import
    Dear PyGui (see `architecture.md`, "UI controller"); they are unit-tested against `SimulatedLink`.
  - The DPG modules only render `controller.state` and call controller intents.
- **Layout** (`ui/app.py` `App`):
  - toolbar (`toolbar.*`): port combo + Connect/Disconnect, Live/Scan radio, preset combo, resolution combo,
    Start/Stop, Reset max hold
  - spectrum plot over the waterfall, in one `subplots(2, 1, link_all_x=True)` so the MHz axes line up
  - right-hand tab bar (`tabs`, width 370): Device | Scan | Markers | Analysis | Coordination | Profiles; the last
    two show "Coming soon" until their tasks land
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
  - markers, threshold line and reference traces: see "Markers, threshold, references" below
  - channel overlay, exclusion zones: `overlay.py`, see "Channel overlay, analysis" below
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
  M adds a marker at the cursor (at the peak of the main trace when the mouse is not over the plot), P moves the
  selected marker to the peak, N / Shift+N to the next peak right / left; ignored with Ctrl or Alt held
  (`Shortcut.shift` selects the Shift variant). (planned) Ctrl+S save session, Ctrl+E export.
- **Markers, threshold, references** (Task 14):
  - Pure math in `core/markers.py` (`Marker(id, freq_hz, trace_key)`, `level_at` nearest bin or `None` outside the
    trace, `peak`, `next_peak(trace, from, "left"|"right", min_prominence_db=3.0)` built on `find_peaks`,
    `delta(a, b, traces)` = a minus b as `(df_hz, ddb)`); `auto_scale_limits(traces, padding_db=5)` is in
    `core/traces.py`.
  - State (`AppState`): `markers` (max 8, ids 1..8, smallest free id reused), `selected_marker`, `delta_reference`,
    `threshold_dbm` (persisted in settings, `None` = hidden), `references` (`ref1`..`ref4` -> `Trace`),
    `hidden_traces`, `y_limits` + `y_limits_version`, `cursor_hz` (written by `SpectrumView` while the mouse is over
    the plot, `None` otherwise; a deliberate view-to-state hand-off that bumps no version). `trace_map()` lists
    every plottable trace including references. Marker, threshold and selection changes bump `ui_version`;
    reference / visibility changes bump both `trace_version` and `ui_version`.
  - Controller intents: `add_marker(freq_hz)` (selects; `None` and a message at 8), `add_marker_at_cursor`,
    `add_marker_at_peak`, `move_marker(id, freq_hz)` (used by dragging), `remove_marker`, `clear_markers`,
    `select_marker`, `marker_to_peak`, `marker_next_peak(direction)`, `set_delta_reference(id|None)`,
    `marker_rows()` -> `MarkerRow(marker, level_dbm, delta)`, `set_threshold_dbm(value|None)`,
    `freeze_reference()` (copies the main trace = `resolve_trace("max")`: max hold, else scan, else live; refuses
    with a message at 4), `remove_reference(index)` (position in the list), `set_trace_visible(key, bool)`,
    `auto_scale()`. A marker reads the trace named by its `trace_key` (default `max`) and falls back to max hold,
    scan, live when that trace is missing.
  - **Placing a marker: Ctrl+click on the plot** (or the M key / "Add at peak"). Plain click-drag pans and the
    wheel zooms (ImPlot defaults; double-click fits), so a plain click or double-click was not usable.
  - Rendering (`SpectrumView`): a pool of 8 vertical `drag_line` + `plot_annotation` pairs shown/hidden per marker,
    annotation text `M1 612.350 MHz -67.2 dBm` (ASCII hyphen: the default font has no minus sign, same for
    "Delta"); dragging a line calls `move_marker` + `select_marker`; the selected marker is drawn thicker/brighter.
    Lines are only written from state when they differ (tolerance 1e-5) so dragging never fights `set_value`.
    The threshold is a horizontal `drag_line` (red), dragging calls `set_threshold_dbm`. Reference traces are
    4 muted line series (`spectrum.trace.ref1..4`). The legend buttons are disabled (`no_buttons`): visibility is
    state (`hidden_traces`), set from checkboxes in the Markers tab. Auto-scale applies `y_limits` like the range
    (locked 3 frames, then released so zoom works).
  - `panels/markers.py` `MarkersPanel` (tab "Markers"): Add at peak / Clear all / Auto-scale, a table with two
    rows per marker (M select, MHz, dBm, Delta vs ref; then Peak, < >, Set ref, Delete), threshold checkbox + dBm
    input (Enter applies; remembers the last value, default -90), visible-trace checkboxes, "Freeze current trace"
    and one row per reference (visibility checkbox + Remove). Pooled widgets (`markers.row.N`, ...) are shown or
    hidden; the panel refreshes when `ui_version` or `trace_version` changes.
- **Channel overlay, analysis** (Task 15):
  - `ui/overlay.py` `OverlayView` (owned by `SpectrumView`, `spectrum.overlay`): all pooled, rewritten only when
    its key (overlay on, plan name, analysis object, zones) changes. Channel edges = one `inf_line_series`
    (`overlay.grid`, added before the traces); band / span shading and exclusion zones = `draw_rectangle` pools
    (`overlay.span.N` x24, `overlay.zone.N` x16) on a `draw_layer` inside the plot (plot coordinates, +/-1000 dBm
    tall so the plot clips them; they never affect the plot fit; an *outline* on such a tall rectangle renders as a
    fat bar, so there is none). Channel numbers = clamped `plot_annotation` pool (`overlay.channel.N`, x64) pinned
    to the top (y = 1000), background colour = occupancy (`occupancy_color`: grey no data, green < 1 % of bins
    above the threshold, amber < 25 %, red above); zone labels `Xn` are pinned to the bottom. Clamping would pin
    off-screen annotations to the plot edge, so each frame `get_axis_limits(x)` is compared and annotations whose
    centre is outside the view are hidden. Band colours: allowed green, forbidden red, info blue (alpha ~35-45).
    Zones are drawn even with the overlay off; the overlay itself starts off (`overlay_enabled` is state, not
    persisted).
  - `panels/analysis.py` `AnalysisPanel` (tab "Analysis"): overlay checkbox + plan combo (`channel_plans.available()`),
    amplitude offset input (dB, Enter), exclusion zones (new start/stop MHz + Add, then one editable row per zone
    with Delete; Enter applies), detected carriers table (MHz, dBm, Ch, "Add marker"; strongest
    `MAX_CARRIER_ROWS` = 32) and a collapsing "Channel occupancy" table. Tables refresh only when
    `Controller.analysis()` returns a new object (it is cached by trace_version, trace key, threshold, plan).
  - **Exclusion zones are added with the start/stop inputs, not by mouse:** plain drags pan and modified clicks are
    claimed by ImPlot/the marker Ctrl+click, and a mouse gesture cannot be tested headless. State:
    `AppState.exclusion_zones` (`ExclusionZone(id, start_hz, stop_hz)`, ids 1..16, smallest free reused);
    persistence comes with the sessions (Task 16).
  - Controller intents: `set_overlay_enabled`, `set_channel_plan(name)`, `add_exclusion_zone(start, stop)` (either
    order; `None` and a message when empty/at the limit), `update_exclusion_zone`, `remove_exclusion_zone`,
    `clear_exclusion_zones`, `analysis()` -> `Analysis(key, trace_label, floor_dbm, threshold_db,
    from_threshold_line, carriers: tuple[CarrierRow(carrier, channel)], occupancy)`. The analysed trace is the main
    trace (`resolve_trace("max")`); threshold = threshold line when shown (`threshold_dbm - floor`), else floor + 10 dB.
  - **Amplitude offset:** `Controller.amp_offset_db` / `set_amp_offset_db(db)` (+/-50 dB), kept in
    `AppState.amp_offsets` and the `amp_offsets` setting under `amp_offset_key()` = `model_<code of the active
    module>`. `Link` exposes no serial number (only `SerialLink.active_port`), so it is per model code, not per
    unit. The offset is added to each live sweep, the scan result and the scan partial before `TraceSet` / the
    waterfall (the device's own offset stays inside the levels `make_sweep` returns, untouched). Changing it clears
    the traces and the waterfall (old data used another offset). Needs a connected device.
  - **Module switcher:** in the Device tab, shown only when `capabilities.expansion_name` is set: two buttons
    (main / expansion, the active one marked) calling `Controller.switch_module(main)`, which stops acquisition,
    calls `link.switch_module`, clears traces and the waterfall; `_sync_device` drops a preset the new module cannot
    tune. The simulator has no expansion, so this is only covered by a fake link in the tests.
- **Simulator:** `opencoord --simulator` uses `SimulatedLink(sweep_points=512)` (like the WSUB1G+ at Normal/Fine) and
  connects on start; Live over 470-960 MHz is clamped to the 342.37 MHz max span at 512 points.
- **Language:** English strings inline; no i18n. Labels use ASCII hyphens (default font).
- **Testing:** `tests/ui/test_controller.py` and `test_state.py`/`test_views.py` need no display.
  `tests/ui/test_smoke.py` (`@pytest.mark.ui`, skipped without `DISPLAY`/`WAYLAND_DISPLAY`) runs
  `python -m opencoord --smoke-frames 5` in a subprocess and, in-process, drives `App` frames against a
  512-point simulator: Live for 60 frames (asserts trace + waterfall + series data), then a Fast scan of
  470-500 MHz to completion, then a marker, reference, threshold, hidden trace and auto-scale (asserting the
  drag lines, annotation and series), then the channel overlay, an exclusion zone and the analysis panel; it logs fps. Dear PyGui segfaults when a second viewport/context is created in the
  same process, so only one test may open a window in-process.
- **Performance (2026-10-09, 144 Hz Wayland desktop, vsync on):** Live at 512 points ~144 fps (vsync-bound),
  per-frame work ~0.2-0.4 ms; a 6261-point Normal 470-960 MHz scan trace re-pushed every frame still 144 fps.
  Spikes up to ~10 ms only on the first frame and on range changes (full waterfall remap + texture upload).
