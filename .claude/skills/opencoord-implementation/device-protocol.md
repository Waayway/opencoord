# RF Explorer device layer

> **Unverified details are marked ⚠.** Check them against the official *RF Explorer UART API* spec and against
> recorded fixtures from the real WSUB1G+ Slim (`tests/fixtures/`) before relying on them, then remove the ⚠.

## Hardware on hand
- **RF Explorer WSUB1G PLUS SLIM** (about 100 kHz – 960 MHz) via a CP2102N USB-UART bridge,
  VID:PID `10c4:ea60`, `/dev/ttyUSB0` on the dev machine.

## Serial
- 8N1, try **500000** baud first, then **2400**. Detect by sending the config request and waiting for `#C2-M`.
  (500000 verified on the WSUB1G+ Slim, fw 03.39; 2400 fallback ⚠ untested.)
- Port discovery: `serial.tools.list_ports`, prefer VID:PID `10c4:ea60`; the user can override the port.
- Observed on the device: a command sent while it is still processing the previous one may be ignored (a `C0`
  0.3 s after `Cj` got no reply), so wait for the `#C2-F` reply before sending the next command.

## Protocol (`src/opencoord/device/protocol.py`, types in `src/opencoord/core/types.py`)
Sources: the official [UART API spec](https://github.com/RFExplorer/RFExplorer-for-.NET/wiki/RF-Explorer-UART-API-interface-specification)
and [RFExplorer-for-Python](https://github.com/RFExplorer/RFExplorer-for-Python) (LGPL, reference only, no code
copied). Where they disagree, the recorded fixtures win. Fixtures (all WSUB1G+ Slim, model 010, fw 03.39, 500000 baud,
each with a `.json` sidecar):
- **F1** `tests/fixtures/wsub1gplus_config_and_sweeps.bin`: `C0` only; 112-point `$S` sweeps.
- **F2** `tests/fixtures/wsub1gplus_512pt_z_sweeps.bin`: `CJ 0x1f` then `C0`; 512-point `$z` sweeps, EEOT abort.
- **F3** `tests/fixtures/wsub1gplus_set_config_470_700.bin`: `C2-F:0470000,0700000,-020,-110` then `C0`.

### Host → device (builders return `bytes`)
- Framing: `b'#' + bytes([total_len]) + payload`, `total_len` counts the `#` and the length byte (F1–F3).
- `request_config()` → `#\x04C0` (F1). The reply is: a banner text line `RF Explorer 03.39 21-Jun-22 …`,
  `#Sn<serial>`, `#CAL:…`, `#BAT:…`, `#C2-M:…`, `#QA:…`, `#LFLO2`, `#C2-F:…`, `#a<input stage>`, then sweeps.
- `set_config(start_hz, stop_hz, top_dbm, bottom_dbm)` → `C2-F:SSSSSSS,EEEEEEE,TTTT,BBBB`, kHz (truncated) as
  7 digits, amplitudes as `f"{dbm:04d}"` (`-010`, `0005`); 32 bytes total (F3: device echoes the new `#C2-F`).
- `set_sweep_points(n)`: multiples of 16 up to 4096 → `CJ` + byte `(n-16)/16` (F2: `0x1f` → 512; `0x06` → 112
  restored the device); otherwise 112..65535 → `Cj` + big-endian u16 (verified ad hoc: `Cj 0x0400` → 1024 points,
  `$z` frames, not kept as a fixture).
- `hold()` → `#\x04CH` ⚠ not yet sent to hardware.
- `switch_module(main)` → `#\x05CM\x00` / `#\x05CM\x01` (binary byte per spec) ⚠ needs a unit with an expansion.
- Builders raise `ValueError` on out-of-range input; never sent: reboot, shutdown, baud change, calibration.

### Device → host (parser events)
- `#C2-M:MMM,EEE,FF.FF` → `ModelReply(ModelInfo(main_code, expansion_code|None, firmware))`; 255 = no expansion
  (F1: `#C2-M:010,255,03.39`, WSUB1G+ = 10).
- `#C2-F:` (also `#C2-f:`) → `ConfigReply(DeviceConfig)`. Fields: start kHz, step **Hz**, amp top, amp bottom,
  sweep points, expansion active, mode, min kHz, max kHz, max span kHz, RBW kHz, amp offset dB, calculator mode
  (F1–F3: e.g. `#C2-F:0431000,0090090,-010,-120,0112,0,000,0000050,0960000,0959950,00110,0000,004`). Stored as
  int Hz. Parsed by splitting on commas because widths vary (step may be 8 digits, points 5); RBW/offset/calc are
  `None` on old firmware. Max span depends on the sweep-point count (959950 kHz at 112, 342370 kHz at 512).
- Sweep frames → `SweepData(samples)` (`float32` dBm, `dBm = -byte / 2`), ending `\r\n`:
  - `$S` + u8 = number of points (F1: `$S\x70` = 112; the spec's "(n+1)×16" text is wrong for `$S`).
  - `$z` + big-endian u16 = number of points (F2: `$z\x02\x00` = 512).
  - `$s` + u8 × 16, 0 = 4096 ⚠ (from RFExplorer-for-Python; fw 03.39 uses `$z` for > 255 points).
- Point `i` is at `start_hz + i * step_hz`, so `DeviceConfig.stop_hz = start + (points-1) * step` (F1: 111 × 90090 Hz
  ≈ 10 MHz span; F3: 2072072 Hz step for 470–700 MHz).
- **EEOT** `ff fe ff fe 00`: the device aborts a sweep dump with it (after a reconfig, or when a session closed the
  port mid-sweep, so the next session's first bytes are a partial sweep and/or EEOT). Seen in F1–F3.
- Any other complete text line (`#Sn…`, `#CAL:…`, banner) → `Unknown(line)`, logged at debug.

## Parser design
- `Parser().feed(data: bytes) -> list[Event]`, incremental and stateful; any chunking gives the same frames
  (Hypothesis test over F1–F3).
- A sweep frame is accepted only if `\r\n` sits exactly after the declared count, so payload bytes equal to `#`,
  `$` or `\r\n` are fine. Zero-count headers are garbage; empty lines are skipped.
- After the first `#C2-F` the parser remembers `sweep_points`; a sweep header with any other count is garbage
  straight away (the device always sends `#C2-F` before sweeps of a new size, F2).
- EEOT inside a pending sweep drops that sweep as `ParseError("…EEOT…")`, but only if the EEOT comes before the
  next `#`/`$` after the header, so a spurious header can never swallow real frames that follow it.
- On garbage (non-text bytes, binary in a `#` line, unknown `$X`, missing terminator, > 512-byte line) it emits
  one `ParseError(reason, data)` and resyncs at the next `#` or `$`. It never raises.
- Known limit: **before the first `#C2-F`**, a spurious `$s`/`$z` header in garbage stalls output until its
  declared length has arrived (≤ 65541 bytes, ~1.3 s at 500 kbaud); then everything after it is re-parsed and
  nothing valid is lost. With a config known, the stall is at most one real sweep length.
- `make_sweep(config, samples, timestamp) -> Sweep` builds the `float64` Hz axis, adds the device `amp_offset_db`
  (as RFExplorer-for-Python does ⚠ only seen as 0), and raises `ValueError` if the length != `config.sweep_points`.

## Models (`models.py`)
- Table keyed by model code. Codes (spec + RFExplorer-for-Python `eModel`): 433M 0, 868M 1, 915M 2, WSUB1G 3,
  2.4G 4, WSUB3G 5, 6G 6, WSUB1G+ 10 (verified, F1), AudioPro 11, 2400+ 12, 4G+ 13, 6G+ 14, W5G3G 16, W5G4G 17,
  W5G5G 18, RFGen 60, RFGen expansion 61, none 255. ⚠ all but 10 and 255 unverified on hardware.
- `Capabilities` are merged from the table hint and the live `#C2-F` reply; the reply wins.
- Implemented in `src/opencoord/device/models.py`: `MODELS` (code -> `ModelHint`) and `resolve(ModelInfo, DeviceConfig|None) -> Capabilities`
  (active module: expansion hints only when `config.expansion_active`; config min/max/span win; exposes `main_name`/`expansion_name`).
  Ranges other than code 10 are spec-derived hints, not hardware-verified.
- Unknown codes: the app keeps working using the reported limits and shows "Unknown model (code N)".

## Segmented scanning (`scanner.py`)
- RBW grows with span, so wide hi-res views are built from narrow segments (default 10–20 MHz, set by a "resolution"
  preset).
- Per segment: set span → discard the first sweep after reconfig → collect N sweeps → max-hold → append.
- Stitch: concatenate on a common Hz axis; drop overlap duplicates and keep the max.
- Emits progress and partial traces so the UI fills in left to right.

## Simulator (`simulator.py`)
- Implemented in `src/opencoord/device/simulator.py`; the shared interface is the `Link` Protocol in
  `device/link_api.py` (`open/close/set_span/hold/switch_module`, `model/config/capabilities/is_open`,
  `sweeps`/`events` queues carrying `Sweep`/`LinkEvent`).
- `SimulatedLink(seed, sweep_points=112, sweep_interval_s=0.1, queue_size=64)`: WSUB1G+ (code 10, fw 03.39), daemon
  thread, queues drop the oldest item when full. `set_span` clamps to capabilities, builds a new `DeviceConfig`
  (step = round(span/(points-1))) and resumes after `hold()`. `switch_module(False)` emits an `error` event (no expansion).
- `generate(start_hz, stop_hz, points, t, seed) -> float32 dBm` is pure/deterministic: DVB-T ch 22/27/35 (-60 dBm,
  8 MHz), FM carriers in 563-831 MHz, intermittent carrier at `INTERMITTENT_HZ` (on while t % 4 < 2).
- Synthetic spectrum: noise floor around −105 dBm ± jitter, DVB-T 8 MHz blocks, narrowband FM carriers, an optional
  intermittent carrier.
- Deterministic with a seed (for tests). Selected with `opencoord --simulator`.

## Errors
Each error maps to a user message with OS-specific help:
- **Permission denied:** Linux `uucp`/`dialout` group or the udev rule
- **Port busy:** close RF Explorer for Windows / other apps
- **No device:** CP210x driver links for Windows/macOS
- **Unplug:** auto-reconnect with backoff
