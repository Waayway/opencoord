# UI (Dear PyGui, planned)

- **Dear PyGui 2.x.** One viewport. `ui/app.py` builds the layout:
  - toolbar (connect, preset, start/stop, export)
  - spectrum plot over the waterfall
  - a right-hand tab bar: Device | Scan | Markers | Coordination | Profiles
  - a status bar
- **Render loop:** `ui/app.py` uses a manual loop
  (`while dpg.is_dearpygui_running(): drain_queues(); update_plots(); dpg.render_dearpygui_frame()`) so queue draining
  is explicit and testable.
- **Spectrum** (`spectrum.py`):
  - one `line_series` per trace (live/max/avg/min/reference); updates use `dpg.set_value` with numpy → list
  - X axis in MHz, Y axis in dBm
  - TV channel overlay drawn as plot annotations / `drag_rect`-style shaded areas
  - markers use `drag_line` + `annotation`
  - the threshold uses a horizontal `drag_line`
- **Waterfall** (`waterfall.py`): a `dynamic_texture` (RGBA float array, width = trace points, height = history rows).
  It is shifted one row per sweep, colour-mapped with a precomputed LUT, and shown with `image_series` inside a plot so
  the axes line up with the spectrum.
- **Item tags:** string tags namespaced per panel (`"spectrum.trace.max"`, `"coord.results.table"`).
- **Theme:** dark by default (`theme.py`) with a colour-blind-safe trace palette.
- **Shortcuts** (`shortcuts.py`): Space start/stop, R reset max-hold, M marker, P peak, N next peak, Ctrl+S save
  session, Ctrl+E export.
- **Language:** English strings inline; no i18n.
- **Testing:** the smoke test creates the viewport against `SimulatedLink`, renders about 30 frames under `xvfb-run` in
  CI, and asserts there are no exceptions and the traces got data.
