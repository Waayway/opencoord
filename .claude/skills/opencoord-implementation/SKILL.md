---
name: opencoord-implementation
description: How OpenCoord (RF Explorer spectrum scanner + wireless mic/IEM frequency coordinator, Python/uv/Dear PyGui) is implemented — architecture and layering rules, RF Explorer serial protocol, coordination engine, UI, tooling (uv, Nix flake, Docker, PyInstaller, GitHub Actions) and conventions. Use before writing, changing or reviewing any code, packaging or CI in this repo, and update it whenever an implementation detail changes.
---

# OpenCoord implementation guide

OpenCoord is a GPL-3.0, cross-platform desktop app that talks to **RF Explorer** spectrum analyzers over USB serial,
shows the spectrum (live, max-hold, waterfall, markers, TV channel overlay), exports scans, and computes
**intermod-free frequency plans** for wireless mics and IEMs, including user-defined amateur/generic devices.

- Design & phased plan: `plans/2026-10-09-opencoord-plan.md` (source of truth for *what* to build).
- This skill: source of truth for *how* it is built. **Keep it in sync.** Any commit that changes an implementation
  detail described here updates the relevant file in the same commit.

> Status: all v0.1 features are implemented; the release is waiting for the maintainer to bump the version and tag.
> Any section still marked **(planned)** describes a decided design that is not yet in code. When implementing it,
> replace "(planned)" with what was actually built, including file paths.

## Golden rules
1. **Layering:** `device/protocol.py`, `device/models.py`, `core/traces.py` and all of `coord/` are **pure**: no
   serial, no file I/O, no Dear PyGui imports. I/O lives in `device/link.py`, `io/`, `core/session.py`, `core/settings.py`;
   GUI lives only in `ui/`.
2. **Threads:** only the serial worker touches the port, and only the main thread touches Dear PyGui. They communicate
   via `queue.Queue` of immutable `Sweep` objects. Long computations (solver, big exports) run in a worker thread and
   report back through a queue.
3. **Device limits come from the device:** model table values are hints; the `#C2-F` config reply wins.
4. **Units:** frequencies in code are **Hz as `int`** (or `float64` numpy arrays in Hz). Convert to MHz/kHz only at
   the UI/IO edge. Levels are **dBm `float`**.
5. **TDD** for all pure modules; the simulator replaces hardware in tests and CI.
6. **uv only:** never `pip install` into the project. Use `uv add`, `uv run`, `uv sync --locked`.
7. **UI is English only**, so there is no i18n layer.

## Reference files (read the one you need)
| File | Covers |
|---|---|
| `architecture.md` | package layout, data flow, threading, sessions, settings paths |
| `device-protocol.md` | RF Explorer serial protocol, model table, link/reconnect, segmented scanning, simulator |
| `coordination.md` | device profiles (TOML), spacing presets, IMD math, solver algorithm, channel plans |
| `io-formats.md` | sessions (`.opencoord`), CSV/PNG exporters, importers, WWB / WSM / RF Explorer format research with confidence levels |
| `ui.md` | Dear PyGui structure, plots, waterfall texture, shortcuts, theme |
| `tooling-and-packaging.md` | uv, ruff/mypy/pytest, Nix flake, Docker stages, PyInstaller + per-OS packaging, GitHub Actions |
| `conventions.md` | code style, naming, testing patterns, fixtures, commits, how to update this skill |
