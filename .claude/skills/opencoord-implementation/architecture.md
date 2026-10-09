# Architecture

## Package layout
```
src/opencoord/
  __main__.py        python -m opencoord → ui.app.main()
  cli.py             opencoord-cli: info | sweep | scan (debug / headless)
  device/
    protocol.py      pure: command builders + incremental byte parser → events (SweepData, ConfigReply, ModelReply, Unknown, ParseError); make_sweep()
    models.py        pure: model code → Capabilities (name, min/max Hz, max span, sweep-point limits, plus/expansion)
    link.py          SerialLink thread: discover, open, baud detect, request config, stream, reconnect
    scanner.py       SegmentedScanner: drives a link (or simulator) across segments, stitches traces
    simulator.py     SimulatedLink: same interface as SerialLink, synthetic spectra
  core/
    types.py         Sweep, DeviceConfig, ModelInfo (done); Trace, Marker, Band, … (planned)
    traces.py        pure: TraceSet (live/max/avg/min), noise floor, peak detection
    session.py       Session load/save (.opencoord zip: session.json + traces.npz)
    settings.py      AppSettings via platformdirs (config dir: profiles/, presets.toml, settings.toml)
  coord/
    profiles.py      DeviceProfile, SpacingRules, TOML load/save, templates
    imd.py           pure numpy intermod product generation
    solver.py        pure: solve(request) → Plan
    channel_plans/   *.toml region data (eu.toml first)
  io/
    export_scan.py, export_plan.py, importers.py
  ui/
    app.py, spectrum.py, waterfall.py, theme.py, shortcuts.py, panels/*.py
```
Implemented so far: `__init__.py` (`__version__`), `__main__.py`, `cli.py`, `ui/app.py`; the rest of the tree
is still to be written. `io/` is deliberately named like the stdlib module; all imports are absolute so it is safe.

The project uses the `src/` layout so tests always run against the installed package.

## Data flow
```
SerialLink thread ──Sweep──▶ queue ──▶ UI frame loop ──▶ TraceSet.update() ──▶ plot series / waterfall texture
                                             │
                     SegmentedScanner ◀──────┘ (owns segment schedule, emits stitched Trace + progress)
UI "Coordinate" ──▶ worker thread: solver.solve(profiles, trace, exclusions, rules) ──▶ queue ──▶ results panel
```

## Threading rules
- `SerialLink` owns the `serial.Serial` object; public methods (`set_span`, `hold`, `switch_module`) enqueue
  commands that the worker thread writes.
- The UI never blocks: it drains queues with `get_nowait()` once per frame.
- Shutdown: `link.close()` sets a stop event; the thread joins with a timeout.

## Persistence
- User data dir (`platformdirs.user_config_dir("opencoord")`): `settings.toml`, `profiles/*.toml`, `presets.toml`.
- Sessions are user-chosen files: `.opencoord` = zip with `session.json` (schema-versioned) + `traces.npz`.
