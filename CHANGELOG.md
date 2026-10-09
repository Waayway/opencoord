# Changelog

## [0.1.0] - unreleased
First release. The scanner is tested on an RF Explorer WSUB1G+ (other models are "community-tested"); the
Shure WWB and Sennheiser WSM CSV exports are built from public format notes and unverified.

### Added
- RF Explorer support over USB serial (protocol parser, model table, reconnecting link, simulator for work without
  hardware), live spectrum with average / min / max traces, waterfall, markers with peak search and delta,
  threshold line, reference traces and amplitude offset per model.
- Segmented wide-band scans (Fast / Normal / Fine) stitched into one trace, with range presets.
- Channel overlay with the EU / Netherlands DVB-T plan and band annotations, per-channel occupancy, exclusion
  zones and a detected-carrier list (Analysis tab).
- Recording (`.ocrec`, crash-recoverable) with replay at 1x / 4x / max, and a long-run max-hold CSV logger with
  threshold alerts (Record tab).
- Sessions (`.opencoord`) holding traces, markers, zones, settings, the coordination setup and the plan.
- Exports: scan CSV (generic, Shure WWB, Sennheiser WSM; WWB and WSM unverified), detected carriers CSV, plot PNG;
  import of generic and RF Explorer single-signal CSV as reference traces.
- Coordination: device profiles (TOML, editor, templates), editable spacing presets, intermod-free solver with
  3rd, 5th and 7th order products, locked carriers, forbidden bands, scan data and exclusion zones, backup
  frequencies, reasons for unassigned devices, check mode for hand-made plans, plan drawn on the spectrum, plan
  exports as CSV, TXT and printable HTML.
- `opencoord --screenshot PATH [--tab NAME]` saves a picture of the window; `docs/make_screenshots.py` regenerates
  the README screenshots from the simulator.
- Packaging: Windows installer + portable zip, macOS DMG (arm64), Linux AppImage + tar.gz (x86_64, aarch64)
  with a udev rule, Nix flake with `nixosModules.default`, Docker image; GitHub Actions for CI, builds, Nix,
  Docker and a draft GitHub Release with `SHA256SUMS` (`release.yml`; PyPI publishing present but disabled).
- Community `profiles/` folder with an example profile, issue templates, CONTRIBUTING and a full README.

### Known limitations
- Windows and macOS builds are unsigned (SmartScreen / Gatekeeper prompts); no Intel macOS build.
- Not published on PyPI, Flatpak or deb/rpm (deferred).
