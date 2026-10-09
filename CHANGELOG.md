# Changelog

## Unreleased
- Coordination tab: devices (profile + quantity), locked carriers (paste several), options (threshold, guard,
  forbidden bands, single group, time budget, run override, backups), background Coordinate with Cancel, result
  table with reasons for unassigned devices, plan lines on the spectrum, check mode for hand-made plans; plan and
  setup saved in the session (format version 2); plan export as CSV, TXT and printable HTML with the spectrum.
- Recording (`.ocrec`, crash-recoverable chunks), replay at 1x/4x/max through `ReplayLink`, and a long-run max-hold CSV logger with threshold alerts (Record tab).
- Project skeleton: uv project, package layout, minimal Dear PyGui window, CLI placeholder.
- Native packaging: PyInstaller onedir, Linux AppImage + tar.gz, Windows Inno Setup installer + zip,
  macOS DMG (`packaging/build.py`, `build.yml`); app icon.
- Channel plans (EU DVB-T 21-48 + band annotations), spectrum overlay with per-channel occupancy, exclusion
  zones, detected-carrier list, amplitude offset per model and expansion module switcher (Analysis tab).
