# Architecture

## Package layout
```
src/opencoord/
  __main__.py        python -m opencoord → ui.app.main()
  cli.py             opencoord-cli: info [--json] | sweep | scan | record (debug / headless)
  device/
    protocol.py      pure: command builders + incremental byte parser → events (SweepData, ConfigReply, ModelReply, Unknown, ParseError); make_sweep()
    models.py        pure: model code → Capabilities (name, min/max Hz, max span, sweep-point limits, plus/expansion)
    link.py          SerialLink thread: discover, open, baud detect, request config, stream, reconnect (done)
    scanner.py       SegmentedScanner (done): non-blocking step() drives a link across segments, stitches traces
    link_api.py      Link Protocol + LinkEvent(kind, message) (done)
    simulator.py     SimulatedLink + pure generate(): same interface as SerialLink, synthetic spectra (done)
  core/
    types.py         Sweep, DeviceConfig, ModelInfo, Trace, Carrier, ExclusionZone (done); Band, … (planned)
    occupancy.py     pure (done): channel_occupancy(trace, channels, floor_dbm, threshold_db) -> [ChannelOccupancy(number, max_dbm, avg_dbm (power mean), percent_above, coverage 0..1)]
    analysis.py      pure (done): analyze(trace, plan, threshold_dbm) -> Analysis (floor, carriers with channel, occupancy)
    zones.py         pure (done): exclusion zone list helpers (add/update/remove/contains, MAX_EXCLUSION_ZONES = 16)
    offsets.py       pure (done): offset_key(model, config) = model_<code>, offset_sweep / offset_trace
    markers.py       Marker (frozen), level_at, peak, next_peak, delta, MAX_MARKERS = 8 (pure, done)
    traces.py        pure (done): TraceSet (live/max/avg/min; average = exact mean of last N, dB domain; axis change resets),
                     noise_floor (20th percentile), find_peaks (own O(n) prominence, strongest first), detected_carriers
    presets.py       pure (done): RangePreset(name, start_hz, stop_hz), PRESETS (plan §4), available(device_range), find()
    session.py       (done) Session/SessionSettings/DeviceInfo, pure to_json/from_json + encode_traces/decode_traces, save/load of the .opencoord zip; SessionError
    settings.py      AppSettings + load()/save() of settings.toml via platformdirs (done)
  coord/
    profiles.py      DeviceProfile, SpacingRules, TOML load/save, templates
    imd.py           pure numpy intermod product generation
    solver.py        pure: solve(request) → Plan
    channel_plans/   __init__ (available/load via importlib.resources), model.py (parse_plan, dataclasses), eu.toml (done)
  io/
    png.py (done: encode_png RGBA8), atomic.py (done: write_atomic), export_scan.py (done), importers.py (done), export_plan.py (planned)
  ui/
    state.py         AppState (what views render, version counters), WaterfallHistory, resample_max (no DPG)
    controller.py    Controller: owns link/TraceSet/scanner/settings; intents + tick(); no DPG (done)
    files.py         FileActions (done): session build/apply, save/open, exports, reference import; no DPG
    file_dialogs.py  FileUI (done): File menu, DPG file dialogs, export window, PNG frame capture
    app.py, spectrum.py, overlay.py, waterfall.py, theme.py, shortcuts.py, panels/{device,scan,markers,analysis}.py (done; see ui.md)
```
Implemented so far: `__init__.py` (`__version__`), `__main__.py`, `cli.py`,
`ui/{app,state,controller,spectrum,overlay,waterfall,theme,shortcuts}.py`,
`ui/panels/{device,scan,markers,analysis}.py`,
`core/{types,traces,markers,occupancy,analysis,zones,offsets,presets,settings,session,logger}.py`, `io/{png,atomic,export_scan,importers,recording}.py`, `ui/{files,file_dialogs,recording}.py`, `ui/panels/record.py`, `device/replay.py`, `coord/channel_plans/`,
`device/{protocol,models,link_api,simulator,link,scanner}.py`; the rest of the tree
is still to be written. `io/` is deliberately named like the stdlib module; all imports are absolute so it is safe.

The project uses the `src/` layout so tests always run against the installed package.

## Data flow
```
SerialLink thread ──Sweep──▶ queue ──▶ UI frame loop ──▶ TraceSet.update() ──▶ plot series / waterfall texture
                                             │
                     SegmentedScanner ◀──────┘ (owns segment schedule, emits stitched Trace + progress)
UI "Coordinate" ──▶ worker thread: solver.solve(profiles, trace, exclusions, rules) ──▶ queue ──▶ results panel
```

## UI controller (`ui/controller.py`)
- `Controller(link_factory, *, settings, port_lister=find_ports, simulator=False)`; `link_factory(port)` builds a
  `Link` (`SerialLink(port)` or `SimulatedLink`). Views call intents (`connect`, `disconnect`, `refresh_ports`,
  `start`, `stop`, `toggle`, `set_mode`, `set_range`, `set_center_span`, `set_preset`, `set_resolution`,
  `reset_max_hold`, `set_waterfall_depth`, `set_auto_connect`, and the marker / threshold / reference / overlay /
  exclusion zone / amplitude offset / module intents listed in `ui.md`) and render `controller.state` (`ui/state.py`
  `AppState`); the frame loop calls `tick()` once per frame. Unit-tested against `SimulatedLink` without DPG
  (`tests/ui/test_controller.py`).
- `open()`/`close()` run on worker threads (they block up to 5 s); results (`_Opened`/`_OpenFailed`/`_Closed`)
  come back through a queue drained by `tick()`. Serial ports open `exclusive=True`, so after `disconnect()` the
  state is `disconnecting` and `connect()` is refused until every old link has reported `_Closed`, including a
  connect abandoned mid-`open()` (closed as soon as its `open()` returns). `shutdown()` sets a flag so a late
  successful `open()` closes its own link, closes the current link and joins close threads (3 s max). `startup()` lists ports and connects only the simulator, or `last_port` when `auto_connect` is on
  (default off): it never probes ports on its own.
- **Live:** `set_span(range)` (device clamps); sweeps are dropped until `link.config` is a new object (the retune is
  confirmed; after 3 s the current config is accepted), then only sweeps matching the config axis are folded into
  `TraceSet` and `WaterfallHistory`. **Scan:** `SegmentedScanner` stepped every tick (it owns `link.sweeps`); its
  `partial` is `state.scan_partial`; the result is fed into `TraceSet` as a sweep (repeated scans accumulate max
  hold) and the waterfall. Stop cancels and keeps stepping until the restore is done (`state.stopping`). Idle:
  `hold()` and discard sweeps.
- **Hooks** (Task 17, keep `controller.py` small): `Controller.on_sweep` (callables `(raw, shown)` per accepted live sweep and per finished scan; `shown` has the amplitude offset, `raw` is what the link delivered), `on_tick(now)` (end of every tick, also when disconnected) and `on_shutdown`; exceptions in a hook are logged, never break acquisition. `ui/recording.py` `RecordingActions(controller, clock=, timestamp=)` registers itself and owns the recorder, replay control and logger (see `ui.md`).
- Versions: `ui_version` (widgets), `trace_version` (plot series), `WaterfallHistory.version` (texture), so views
  push data to DPG only when it changed.

## Threading rules
- `SerialLink` owns the `serial.Serial` object; public methods (`set_span`, `hold`, `switch_module`) enqueue
  commands that the worker thread writes.
- The UI never blocks: it drains queues with `get_nowait()` once per frame.
- Shutdown: `link.close()` sets a stop event; the thread joins with a timeout.

## Persistence
- User data dir (`platformdirs.user_config_dir("opencoord")`): `settings.toml`, `profiles/*.toml`, `presets.toml`.
- `core/settings.py` `AppSettings` keys: `last_port`, `auto_connect` (false), `preset` (`""` = custom range),
  `resolution`, `start_hz`/`stop_hz`, `window_width`/`window_height`, `waterfall_depth` (300, limits
  `WATERFALL_DEPTH_MIN`/`MAX` = 10/1000 shared with the controller and scan panel), `mode` (`live`/`scan`), `threshold_dbm` (optional float, omitted when hidden), `amp_offsets` (`[amp_offsets]` table, device key `model_<code>` -> dB within +/-50; invalid entries dropped, omitted when empty).
  `load()` ignores unknown keys and replaces invalid values by defaults (logged); an unreadable file (OS error,
  bad TOML, invalid UTF-8) gives defaults. `save()` writes atomically (tmp + replace). The app loads on start and saves on exit.
- Sessions are user-chosen files: `.opencoord` = zip with `session.json` (`schema_version` 1) + `traces.npz`
  (`<name>.freqs_hz` float64, `<name>.dbm` float32 per trace name: live/max/avg/min/scan/ref1..ref4). JSON holds
  `opencoord_version`, `created`/`modified` (UTC ISO), `device` (model_name, model_code, firmware or null),
  `settings` (start/stop Hz, mode, preset, resolution, threshold_dbm, overlay_enabled, channel_plan), trace labels,
  `markers`, `exclusion_zones`, `plan` (null, reserved for Task 22). A newer `schema_version` raises
  `SessionError` ("update OpenCoord"); missing optional fields use defaults; bad zip/JSON/arrays raise
  `SessionError` with a user-facing message (corrupt deflate, unsupported compression, missing members, > 256 MB uncompressed). Saves are atomic and fsynced (`io/atomic.py`). Applied content is validated by `FileActions` (<= 8 markers, <= 16 valid zones, unique ids, markers on unknown traces dropped). See `io-formats.md`.
