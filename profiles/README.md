# Community device profiles

Profiles describe which frequencies a wireless microphone, IEM or other transmitter can use and which
spacing rules apply to it. OpenCoord's coordinator uses them to compute intermod-free frequency plans.

## Using a profile

1. Open the **Profiles** tab in OpenCoord, press **Import** and choose a `.toml` file from this folder.
2. Pick the profile in the **Coordination** tab.

Or copy the file into OpenCoord's profile folder (`profiles/` inside the user config directory: for
example `~/.config/opencoord/profiles/` on Linux, `%LOCALAPPDATA%\opencoord\opencoord\profiles\` on Windows,
`~/Library/Application Support/opencoord/profiles/` on macOS).

## File format

```toml
[profile]
name = "Generic UHF handheld 823-832"   # unique, shown in the app
kind = "mic"                            # mic | iem | other
tuning = [[823.0, 832.0]]               # MHz ranges, needs step_khz
step_khz = 25
spacing = "generic-analog"              # generic-analog | digital | iem | conservative (or your own preset)

[spacing]                               # optional overrides, kHz, 0 = rule disabled
im3_2tx = 100
```

A profile has exactly one frequency source: `tuning` + `step_khz`, a flat `channels = [MHz, ...]` list,
or `[[groups]]` with `name` and `channels` (fixed channel banks). Mixing them is an error. The built-in
templates in `src/opencoord/coord/profile_templates/` show each variant.

## Contributing a profile

Open a pull request that adds one `.toml` file here, named after the device (lower case, hyphens), or
use the "Device profile" issue template. Please state in a comment at the top of the file where the
tuning range and channels come from (manufacturer data sheet, manual) and whether you verified it on
real hardware. A test (`tests/coord/test_community_profiles.py`) checks that every file in this folder
parses; run it with `uv run pytest tests/coord/test_community_profiles.py`.
