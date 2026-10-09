# RF Explorer device layer

> **Unverified details are marked ⚠.** Check them against the official *RF Explorer UART API* spec and against
> recorded fixtures from the real WSUB1G+ Slim (`tests/fixtures/`) before relying on them, then remove the ⚠.

## Hardware on hand
- **RF Explorer WSUB1G PLUS SLIM** (about 100 kHz – 960 MHz) via a CP2102N USB-UART bridge,
  VID:PID `10c4:ea60`, `/dev/ttyUSB0` on the dev machine.

## Serial
- 8N1, try **500000** baud first, then **2400**. Detect by sending the config request and waiting for `#C2-M`.
- Port discovery: `serial.tools.list_ports`, prefer VID:PID `10c4:ea60`; the user can override the port.

## Framing ⚠
- Host → device: `b'#' + bytes([total_len]) + payload`, where `total_len` includes the `#` and the length byte.
  Example: config request `#\x04C0`.
- Key commands ⚠:
  - `C0`: request config
  - `C2-F:SSSSSSS,EEEEEEE,TTTT,BBBB`: start/end kHz (7 digits), amplitude top/bottom dBm (4 chars incl. sign)
  - `CH`: hold
  - `CM` + module byte: switch main/expansion
  - `CJ` / `Cj`: sweep points
- Device → host:
  - `#C2-M:MMM,EEE,FF.FFFF`: model code, expansion code (255 = none), firmware
  - `#C2-F:…`: start kHz, step Hz, amp top, amp bottom, sweep steps, expansion active, mode, min/max kHz,
    max span kHz, RBW kHz, amp offset, calculator mode
  - `$S` + count (u8) + count bytes / `$s` (extended count) / `$z` + count (u16) + bytes: sweep samples,
    `dBm = -byte / 2`, ending in `\r\n`
  - other `#`-prefixed lines (serial number `#Sn`, etc.) are parsed as `Unknown` and logged

## Parser design
- `protocol.Parser.feed(data: bytes) -> list[Event]`, incremental and stateful, tolerant to partial frames.
- On garbage it resyncs by scanning for the next `$` or `#` at a line start.
- Never raises on bad input; it emits `ParseError` events for logging and tests.

## Models (`models.py`)
- Table keyed by model code ⚠ (433M, 868M, 915M, WSUB1G, 2.4G, WSUB3G, 6G, WSUB1G+, 2400+, 4G+, 6G+, combos).
- `Capabilities` are merged from the table hint and the live `#C2-F` reply; the reply wins.
- Unknown codes: the app keeps working using the reported limits and shows "Unknown model (code N)".

## Segmented scanning (`scanner.py`)
- RBW grows with span, so wide hi-res views are built from narrow segments (default 10–20 MHz, set by a "resolution"
  preset).
- Per segment: set span → discard the first sweep after reconfig → collect N sweeps → max-hold → append.
- Stitch: concatenate on a common Hz axis; drop overlap duplicates and keep the max.
- Emits progress and partial traces so the UI fills in left to right.

## Simulator (`simulator.py`)
- Same public interface as `SerialLink`.
- Synthetic spectrum: noise floor around −105 dBm ± jitter, DVB-T 8 MHz blocks, narrowband FM carriers, an optional
  intermittent carrier.
- Deterministic with a seed (for tests). Selected with `opencoord --simulator`.

## Errors
Each error maps to a user message with OS-specific help:
- **Permission denied:** Linux `uucp`/`dialout` group or the udev rule
- **Port busy:** close RF Explorer for Windows / other apps
- **No device:** CP210x driver links for Windows/macOS
- **Unplug:** auto-reconnect with backoff
