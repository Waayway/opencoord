# UI (Dear PyGui)

- **Dear PyGui 2.x.** One viewport. Logic and rendering are split:
  - `ui/controller.py` `Controller` + `ui/state.py` `AppState` hold all behaviour and state and never import
    Dear PyGui (see `architecture.md`, "UI controller"); they are unit-tested against `SimulatedLink`.
  - The DPG modules only render `controller.state` and call controller intents.
- **Layout** (`ui/app.py` `App`):
  - toolbar (`toolbar.*`): port combo + Connect/Disconnect, Live/Scan radio, preset combo, resolution combo,
    Start/Stop, Reset max hold
  - spectrum plot over the waterfall, in one `subplots(2, 1, link_all_x=True)` so the MHz axes line up
  - right-hand tab bar (`tabs`, width 370): Device | Scan | Markers | Analysis | Record | Coordination | Profiles; Coordination
    shows "Coming soon" until its task lands
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
  (`Shortcut.shift` selects the Shift variant). Ctrl+S / Ctrl+Shift+S / Ctrl+O / Ctrl+E (`shortcuts.bind_files`, work in text fields too) = save / save as / open / export.
- **Files** (Task 16): `ui/files.py` `FileActions(controller)` (no DPG; `path`, `title`, `build_session`, `save(path)`, `open(path)`, `apply_session`, `export(fmt_key, trace_key, path, rgba)`, `import_reference(path)`, `trace_choices()`; every failure becomes a status message and `False`) and `ui/file_dialogs.py` `FileUI` (File menu in the main window's menu bar: Open, Save, Save as, Export, Import scan as reference; modal export window with format + trace combos, then a DPG `file_dialog`; the window title shows the session file name). **Opening a session while connected never touches the device:** acquisition is stopped, saved range/mode/resolution become the selected settings, saved live/max/avg/min traces are shown until new sweeps replace them, references/markers/zones/threshold/overlay are replaced. `opencoord <file.opencoord>` opens at startup. **PNG export:** `dpg.output_frame_buffer(callback=)` 4 frames after the dialog closed (so it is not in the picture), float32 RGBA converted to uint8 and cropped to the plot area (`App.plot_rect()`: readout line + spectrum + waterfall; computed from the readout's position and the layout constants because child windows expose no `rect_min`). Unsaved-changes indicator: not implemented.
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
    two keys: the static one (overlay on, plan name, zones) redraws grid, spans, zones and labels, the analysis object (compared by identity) only recolours the channel badges. Channel edges = one `inf_line_series`
    (`overlay.grid`, added before the traces); band / span shading and exclusion zones = `draw_rectangle` pools
    (`overlay.span.N` x24, `overlay.zone.N` x16) on a `draw_layer` inside the plot (plot coordinates, +/-1000 dBm
    tall so the plot clips them; they never affect the plot fit; an *outline* on such a tall rectangle renders as a
    fat bar, so there is none). Channel numbers = clamped `plot_annotation` pool (`overlay.channel.N`, x64) pinned
    to the top (y = 1000), background colour = occupancy (`occupancy_color`: grey for no data or a channel covered < 50 % (`PARTIAL_COVERAGE`, no verdict), green < 1 % of bins
    above the threshold, amber < 25 %, red above); zone labels `Xn` are pinned to the bottom. Clamping would pin
    off-screen annotations to the plot edge, so each frame `get_axis_limits(x)` is compared and annotations whose
    centre is outside the view are hidden. Band colours: allowed green, forbidden red, info blue (alpha ~35-45); zone fill alpha 70.
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
  - Pure logic lives in `core/analysis.py` (`analyze(trace, plan, threshold_dbm) -> Analysis`, `CarrierRow`,
    `MAX_CARRIER_ROWS`, `DEFAULT_CARRIER_THRESHOLD_DB`; the plan is a structural `PlanLike`), `core/zones.py`
    (`add_zone/update_zone/remove_zone/contains`, `MAX_EXCLUSION_ZONES`; raise `ValueError` with the user message)
    and `core/offsets.py` (`offset_key`, `offset_sweep`, `offset_trace`); the controller only delegates and caches.
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
    the traces and the waterfall (old data used another offset); reference traces and the threshold line keep their old levels (the message says so). Needs a connected device.
  - **Module switcher:** in the Device tab, shown only when `capabilities.expansion_name` is set: two buttons
    (main / expansion, the active one marked) calling `Controller.switch_module(main)`, which stops acquisition,
    calls `link.switch_module`, clears traces and the waterfall; `_sync_device` drops a preset the new module cannot
    tune. The simulator has no expansion, so this is only covered by a fake link in the tests.
- **Record tab** (Task 17; `panels/record.py` `RecordPanel` + `ui/recording.py` `RecordingActions`, no DPG in the latter):
  - Recording: path input (+ `FileUI.ask` dialog), Record / Stop recording, "Recording to x: m:ss, N sweeps", then "Finishing the recording file..." while the writer thread zips (status "Saved recording ..."); a blocking `.parts` folder shows a "Recover the unfinished recording" button (`RecordingActions.recover`, background `FinalizeJob`). `start_recording` needs a connection; the file gets `.ocrec`; sweeps are appended from `Controller.on_sweep` (raw levels, no offset) while Live or a scan runs; recording survives disconnects; stopped/finalised on app exit (`on_shutdown`); disk errors stop it with a message pointing at the `.parts` dir.
  - Replay: Open recording... / Open unfinished recording... (folder; `FileUI.ask(..., directory=True)`) (`open_replay` = `controller.connect("replay:<path>")`, only while disconnected), speed combo 1x/4x/Max, Play/Pause (`toggle_replay_pause`: starts the replay when not running, else pauses), Restart, position bar in %. Shown in Live mode; Scan mode is refused; Start/Stop in the toolbar also play/pause.
  - Logger: checkbox + CSV path, interval (s, >= 1, default 60), alert threshold (own, else the Markers-tab threshold line), range list (<= 8; none = current view range at enable; locked while running) with Add / "Add current view range", status (next row in, rows, alerts) and a red alert line + status-bar text (`state.logger_alert`, cleared by "Clear alert"). `LoggerEngine` is fed the *shown* (offset) sweeps; alerts: log warning + status message + `ALERT` CSV line, debounced per range for one interval; disabling writes the partial interval's rows.
  - The panel caches text/config per tag so a frame without changes makes no DPG call (`_set/_cfg/_value`).
- **Profiles tab** (Task 21; `panels/profiles.py` `ProfilesPanel`, `ui/profiles_actions.py` `ProfilesActions`, `ui/profile_editor.py`;
  the last two have no DPG):
  - `profile_editor.py`: `ProfileDraft` / `PresetDraft` are the editable forms (intent methods: `set_name/kind/preset`,
    `add_range/remove_range/set_range/set_step_khz`, `paste_channels(text, group=None, append=False)`, `tidy_channels`,
    `add_group/remove_group/rename_group`, `set_mode`, `set_override/clear_override`, `clone`). **Drafts never build a
    profile themselves:** `build(preset_names, taken_names)` makes the TOML dict shape (only the selected mode's data) and
    runs `profile_from_dict` / `preset_from_dict`; `validate()` returns readable errors (`friendly()` strips `[profile]`
    table names, tuning ranges are 1-based, a name clash is an error). A draft keeps the data of all three frequency
    sources; `dropped_warning()` says what saving drops. Channel lists are `ChannelList` (free text kept as typed + parsed
    values); `parse_channel_text` splits on newline/comma/semicolon/space/tab, accepts a trailing `MHz`, reports one error
    per bad token, and returns sorted unique Hz (Decimal based, so `470.125` is exact). Separators: newline, `;`, tab, space, comma. A token with a `.` uses the dot as decimal mark and every comma in it separates (`606.5,606.1,606.3` = 3 values); without a dot one comma between digits is a decimal comma (`470,125` = 470.125) and several (`470,125,470,250`) are an "ambiguous" error.
    `preview()` = `Preview(count, span, per_group)`, `effective_spacing(preset_rules)` = per rule preset vs used kHz.
    `dirty` (edited since load/save; clones and imports count as edited), `unsaved` (= dirty or never stored),
    `revision` (every edit), `structure_version` (rows added/removed, mode change) and `uid` (draft identity).
  - `ProfilesActions(store, say)`: `startup()` (`seed_defaults()` + `reload()`), `presets/profiles/issues` (LoadIssue list),
    `draft` / `preset_draft`, `message(+_is_error)`, `pending` (inline confirmation, scope `profile`|`preset`) and `version`.
    Intents: `new_blank`, `new_from_template`, `select_profile`, `clone_profile`, `save_profile` (rename through
    `store.rename_profile`), `request_delete_profile` (confirm first; an unsaved draft is just dropped), `import_profile`
    (opens an unsaved draft; unknown preset falls back to the default one with a note, a taken name gets a suffix),
    `export_profile`, and for presets `new_preset/select_preset/clone_preset/save_preset/request_delete_preset`,
    `request_reset_preset` (asks first) / `reset_preset_to_builtin` (also restores a deleted built-in), `missing_builtin_presets`. Switching away from a dirty
    draft asks first (`pending`). Renaming a preset rewrites the profiles that use it (a failing profile is reported, never aborts: the rename is always finished, marked saved and reloaded; unloadable profile files still naming the old preset are listed); deleting a preset in use (saved profiles, the open draft, load-issue files naming it) is refused. A failed reload is appended to the success message.
  - `ProfilesPanel`: two sub-tabs (Profiles | Spacing presets), load issues in red at the top, the Save / Clone / Export /
    Delete row and the red error list sit right under the header (no scrolling to find them). Callbacks only change the
    draft; `update()` rebuilds `profiles.source` (ranges / channel box / groups) when `(uid, structure_version)` changes
    and otherwise refreshes texts when a version moved, so typing is never interrupted. `is_typing()` is passed to
    `shortcuts.bind(typing=...)` because those inputs are created later. Store root is `default_config_dir()`; the smoke
    run (`--smoke-frames`) uses a temporary folder. `App(..., profile_store=...)` is a required keyword.
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
- **Callbacks run on the UI thread:** `App.build()` calls `dpg.configure_app(manual_callback_management=True)` and
  `App.frame()` runs `dpg.run_callbacks(dpg.get_callback_queue())` right after `controller.tick()`, so ALL DPG
  callbacks (widgets, menus, file dialogs, `output_frame_buffer`, drag lines, key and item handlers) execute on the
  main thread between the tick and the view updates. (By default DPG runs them on its own thread; the smoke test
  asserts the thread id.) Any new code that creates a viewport must keep pumping the queue each frame.
- Opening a session is refused (status "Stop the scan before opening a session") while `state.busy`.
