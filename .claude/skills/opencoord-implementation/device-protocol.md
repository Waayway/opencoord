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
- `hold()` → `#\x04CH`: stops the sweep dump (verified ad hoc on the WSUB1G+, the dump ends with EEOT); a
  `set_config` sent while held resumes sweeping (verified ad hoc), so `set_span` needs no extra `C0`.
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

## Segmented scanning (`src/opencoord/device/scanner.py`)
- RBW grows with span (the device picks it from the step), so wide hi-res views are built from narrow segments.
- `Resolution` (StrEnum `fast`/`normal`/`fine`) → `PRESETS[res] = ScanPreset(segment_span_hz, sweeps_per_segment,
  sweep_points, sweeps_per_s)`. `sweeps_per_s` is measured on the WSUB1G+ and only feeds `estimate_seconds()`
  = segments × (N + 1) / rate (the discarded first sweep costs one period; confirmation is 30–70 ms).
  Measured sweeps/s (fw 03.39): 112 pts 3.35 at 10–50 MHz, 2.44 at 5, 1.93 at 2; 512 pts 0.89 at 40–200 MHz, 0.72
  at 30, 0.61 at 20, 0.47 at 10; 1024 pts 0.45 at 100–200, 0.36 at 50, 0.31 at 40. At equal RBW, 512 points over a
  4–5× wider span is at least as fast as 112 points and needs 4–5× fewer retunes, so Normal/Fine use 512 points:
  Fast 20 MHz × 1 sweep @ 112 (180 kHz bins), Normal 40 MHz × 3 @ 512 (78 kHz bins, RBW 95 kHz),
  Fine 20 MHz × 2 @ 512 (39 kHz bins, RBW 48 kHz). See the table comment in `scanner.py` and plan Q7.
- `plan_segments(start, stop, span, points)` (pure): whole-kHz edges, equal spans, neighbours overlap by
  `OVERLAP_STEPS` = 2 steps (the device truncates its step), last segment shifted to end at `stop`.
- `SegmentedScanner(link, start, stop, resolution, *, settle_timeout_s=10, clock=time.monotonic)` clamps the range to
  the capabilities and needs an open link. If the device cannot do the preset's points (`sweep_points_max`, e.g. 112
  on legacy models) the points are clamped and the segment narrowed to keep the bin width (`sweep_points` property).
  `SegmentedScanner.overview(link, start, stop)` is one segment clamped to `max_span_hz` at the device's current
  point count, one sweep; `range_hz` gives the range actually scanned.
- `step()` never blocks: on the first call it remembers `link.config`, calls `set_sweep_points` if the preset differs
  and `set_span` for segment 0; then it drains `link.sweeps` with `get_nowait()`. A sweep counts only when
  `link.config` **and** the sweep's own axis match the requested segment (point count equal, start/stop within one
  step or 1 kHz). The first matching sweep after each retune is discarded, the next N are max-held. Each retune also
  drops whatever was queued. No progress for `settle_timeout_s` → the segment is requested again (logged), up to
  `MAX_RETUNES` = 3 times; then the scan is abandoned with `stalled=True` (no `result`).
- Returns `ScanProgress(segment_index, segment_count, fraction, partial, done, stalled)`; `partial` is the stitch of
  the finished segments (fills in left to right), `result` the final `Trace(label="scan")` (set as soon as the last
  segment is in). `cancel()` stops early (`result` stays `None`).
- After the last segment (or cancel/stall) `step()` restores the device in phases and only then reports `done`:
  `set_sweep_points(original)` first (more points shrink the max span, so restoring the span earlier would be
  clamped), then `set_span(original)` once the points are confirmed. Each phase only accepts a config object newer
  than the one current when its command was sent (configs are replaced on every confirmation, and earlier queued
  retunes may still be confirmed in between). Gives up after `settle_timeout_s`. Callers that close the link (the
  CLI) must keep stepping until `done`.
- While a scan runs it owns the link: the UI must not read `link.sweeps` itself.
- `stitch(traces)` (pure): joins segments left to right keeping every point's measured frequency; points of a later
  segment within half a step of the joined trace's end are merged into the nearest existing point (max kept), the
  rest appended. Output frequencies are exactly device frequencies (they feed the coordination solver); spacing is
  only slightly irregular at segment joins. Plan edges are whole kHz, so they are not on one global step grid.
- `opencoord-cli scan --start MHZ --stop MHZ --resolution {fast,normal,fine} [--csv FILE]` runs it to completion,
  writes `MHz,dBm` rows and a summary line (points, bin width, segments, time, estimate) on stderr.

## Simulator (`simulator.py`)
- Implemented in `src/opencoord/device/simulator.py`; the shared interface is the `Link` Protocol in
  `device/link_api.py` (`open/close/set_span/set_sweep_points/hold/switch_module`, `model/config/capabilities/is_open`,
  `sweeps`/`events` queues carrying `Sweep`/`LinkEvent`).
- Link contract (docstring of `Link`): `config` changes only on device confirmation (async; the simulator applies a pending span on its worker before the next sweep); `set_span` clamps silently, `ValueError` if start>=stop, `RuntimeError` if not open, resumes after `hold()`; `set_sweep_points(n)` is confirmed the same way, keeps start and span and resumes after `hold()`, `ValueError` if not encodable or above `sweep_points_max` (`link_api.check_sweep_points`), `RuntimeError` if not open; `open()` blocks for model+config (timeout 5 s) then emits `connected`, else `ConnectionError` + `error` event; queues are not cleared on close; sweeps may predate the latest `set_span`, so check `sweep.start_hz/stop_hz`; non-retunable links set `retunable = False` (Protocol property; `getattr(link, "retunable", True)` for links that lack it; SerialLink/SimulatedLink True) and may raise `NotImplementedError` from `set_span/set_sweep_points/hold/switch_module`.

## Replay (`replay.py`, Task 17)
- `ReplayLink(path)` plays a `.ocrec` (or an unfinalised `.ocrec.parts` dir) through `Link`; port string `replay:<path>` (`replay_port/replay_path`) makes `Controller.connect` build it, so it uses the normal connect path. `open()` reads meta + first sweep (errors -> `ConnectionError` with the `RecordingError` text), emits `connected`, then waits **paused**. `model/config/capabilities` come from the recording's device info; `config` is re-synthesised (replaced, not mutated) whenever an emitted sweep has another axis (start/step/points). `retunable = False`.
- Pure `ReplayScheduler(sweeps, total)`: `due(now, limit)`, `wait_s(now)`, `set_speed(x, now)` (1, 4, `math.inf` = max), `set_paused`, `position`; recorded gaps are capped at `MAX_GAP_S` = 2 s and a stalled schedule restarts from now (no burst). `ReplayLink.pump()` is one worker step (tests call it with a fake clock and `threaded=False`).
- Deviations from the Link queue rule: `sweeps` has **backpressure** (nothing is dropped; 256 slots). End of recording = one `LinkEvent("disconnected", "End of recording")`, the link stays open (`ended`); `rewind()` starts over (paused). Mid-file damage = `error` event + `disconnected` "Recording ended early".
- Controller: `st.retunable`; Scan mode refused (`set_mode`, `_start_scan`) and a saved scan mode dropped on connect; the live filter that discards sweeps predating a retune is skipped; Start/Stop unpause/pause the replay (`_start_live`/`_hold`, rewinding when ended); the end event is handled after the queued sweeps are folded in (`running` False, traces stay, status "End of recording", connection stays `connected`).
- `SimulatedLink(seed, sweep_points=112, sweep_interval_s=0.1, queue_size=64)`: WSUB1G+ (code 10, fw 03.39), daemon
  thread, queues drop the oldest item when full. `set_span` clamps to capabilities, builds a new `DeviceConfig`
  (step = round(span/(points-1))) and resumes after `hold()`. `set_sweep_points` is applied on the worker before the
  next sweep (keeping start, stop clamped to the new max span) and also resumes after `hold()`. Max span shrinks with
  points like the device: `max_span_for(points)` = min(full range, 342.37 MHz × 512 / points) (959.95 MHz at 112,
  342.37 MHz at 512); capabilities are re-resolved on every config change. `switch_module(False)` emits an `error` event (no expansion).
- `generate(start_hz, stop_hz, points, t, seed) -> float32 dBm` is pure/deterministic: DVB-T ch 22/27/35 (-60 dBm,
  8 MHz), FM carriers in 563-831 MHz, intermittent carrier at `INTERMITTENT_HZ` (on while t % 4 < 2).
- Synthetic spectrum: noise floor around −105 dBm ± jitter, DVB-T 8 MHz blocks, narrowband FM carriers, an optional
  intermittent carrier.
- Deterministic with a seed (for tests). Selected with `opencoord --simulator`.

## Serial link (`src/opencoord/device/link.py`)
- `SerialLink(port=None, *, serial_factory=open_serial, port_lister=list_serial_ports, baud_rates=(500000, 2400),
  command_timeout_s=1.0, reconnect_delay_s=0.5, reconnect_max_delay_s=5.0, stall_timeout_s=10.0, platform=sys.platform)`
  implements `Link`. `SerialLike`/`PortInfoLike` Protocols describe the bits of pyserial it uses; tests inject a
  `FakeSerial` that replays F1 and echoes `C2-F` like F3 (`tests/device/test_link.py`).
- `find_ports(lister) -> list[SerialPort(device, description, is_rf_explorer)]`, `10c4:ea60` first (for a port picker).
  Auto-connect (no `port`) only probes `10c4:ea60` ports, so unrelated serial devices never get `C0` written to them.
- `open(timeout_s=5.0)`: the 5 s budget is shared over all port × baud attempts. Per attempt: open the port
  (`exclusive=True` so a busy port fails on POSIX), write `C0`, read until `#C2-M` then `#C2-F`; resend `C0` once at
  half the budget (busy device). A port that fails to open (any exception) does not stop the search;
  if nothing answers, the first open error is reported, else `no_reply` naming all ports and bauds tried.
  Events after the config are handed to the worker. Opening takes ~150 ms on hardware.
- Worker thread owns the port after `open()`: writes queued commands one at a time; `set_config` waits for any
  `#C2-F` (timeout `command_timeout_s`, one resend, then an `error` event and the next command); `hold`/`switch_module`
  do not wait. Sweeps become `Sweep` via `make_sweep` only if their count matches the current config.
- Reconnect: a read/write `OSError` (pyserial's `SerialException` is one) or no data for `stall_timeout_s` while not
  held → close, `disconnected` event, retry `_connect` with `backoff_delays(0.5, 5.0)` (0.5, 1, 2, 4, 5, 5 …),
  re-discovering the port when none was given, then `connected`. Queued commands survive a reconnect, and an
  unconfirmed `set_config` in flight is re-sent first. The stall clock restarts on every write, so a long hold
  followed by `set_span` does not look like silence.
- `set_sweep_points(n)` queues `protocol.set_sweep_points(n)` as a command confirmed by `#C2-F` (like `set_config`),
  so a following `set_span` waits for it; the new config resolves new capabilities (max span shrinks with points).
  Like `set_config` it clears the worker's `holding` flag (CJ is followed by a new sweep dump, F2).
- `set_span` clamps like the simulator (min span `max(points-1, 1000)` Hz because `C2-F` is in whole kHz) and keeps
  the device's current amplitude top/bottom.
- Hardware test `tests/device/test_link_hardware.py` (`OPENCOORD_HARDWARE=1`, optional `OPENCOORD_PORT`): model +
  3 sweeps, `set_span(470, 700 MHz)`, 3 sweeps in range, then restores the original span.

## Errors
`classify_error(exc, platform) -> "permission" | "busy" | "not_found" | "other"` (by errno on POSIX, by the
`WinError` text on Windows, where "Access is denied" means the port is busy) and
`user_message(kind, port, platform, detail="")` (kinds above plus `"no_reply"`) are pure and unit-tested:
- **Permission denied:** Linux `dialout` (Debian/Ubuntu/Fedora) / `uucp` (Arch) group or the udev rule
  `99-opencoord-rfexplorer.rules`
- **Port busy:** close RF Explorer for Windows / Touchstone / Wireless Workbench / other serial tools
- **No device:** CP210x driver link (silabs.com) on Windows/macOS; `dmesg` hint on Linux
- **No reply:** tried 500000 and 2400 baud; switch it on, leave the device menu
- **Unplug:** `disconnected` event, auto-reconnect with backoff, `connected` event
