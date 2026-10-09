# Import / export formats

Code: `core/session.py`, `io/export_scan.py`, `io/importers.py`, `io/png.py`, `io/atomic.py` (UI side: `ui/files.py`).
All encode/decode functions are pure (`str`/`bytes` in and out); thin `write_*`/`import_file`/`save`/`load` do I/O.
Files are UTF-8 with `\n` line endings; writes are atomic.

## Session (`.opencoord`)
See `architecture.md` "Persistence". Pure: `to_json`, `from_json(text, arrays)`, `encode_traces`, `decode_traces`,
`build_traces`. Round-trip tests: `tests/core/test_session.py` (incl. Hypothesis on the JSON).

## Recording (`.ocrec`, `io/recording.py`, Task 17)
Zip: `meta.json` + `chunk_000000.npz`, ... (chunks stored, already compressed). `meta.json`: `schema_version` 1 (newer -> `RecordingError` "update OpenCoord"), `opencoord_version`, `created`, `sweep_count`, `chunks` [{name, sweeps}], `device` {model_name, model_code (code of the *active* module), expansion_code (null), firmware, min_hz, max_hz, amp_top_dbm, amp_bottom_dbm} or null. Chunk (up to 256 sweeps): `t` float64 (`Sweep.timestamp`, wall clock), `start_hz`/`step_hz`/`points` int64, `irregular` uint8, concatenated `dbm` float32, `freqs_irregular` float64 (frequencies of the sweeps whose axis is not exactly `start + i*step`, e.g. a stitched scan; empty otherwise). Sweeps may differ in axis. Pure: `encode_chunk/decode_chunk/meta_to_json/meta_from_json`.
- `RecordingWriter(path, info, clock=)`: `append`, `poll()` (call every frame), `flush`, `close()`. A chunk is written every 256 sweeps or 5 s (checked in `append`/`poll`) into `<name>.ocrec.parts/` (each file atomic: `chunk_NNNNNN.npz`, then `meta.json`); `close()` flushes, zips to a temp file next to the target, fsyncs, `os.replace`s and deletes the parts dir. An existing non-empty parts dir (unfinished earlier recording) is refused, never overwritten.
- **Crash recovery:** the parts dir survives; `RecordingReader.open` accepts the dir or the final name when only the dir exists (chunks present but not yet in `meta.json` are counted too), so such a recording replays directly (the Open recording dialog filters `.ocrec`, `.*` shows the folder; `finalize_parts(dir)` zips it). At most the unflushed 256 sweeps / 5 s are lost.
- Read limits: 8 GiB total / 256 MiB per chunk uncompressed, <= 2^20 points per sweep, bad zip/JSON/chunk -> `RecordingError` (user-facing; a damaged chunk is reported when it is reached). Reading streams one chunk at a time (`RecordingReader.sweeps()`).
- Step is stored as an integer Hz; a non-uniform axis is stored exactly via `freqs_irregular`.

## Logger CSV (`core/logger.py`, Task 17)
`timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz` (UTC ISO seconds; MHz 6 decimals, dBm 1 decimal): one row per range every interval = max level and its frequency since the previous row (ranges with no data in the interval get no row). Alerts append a line with the same five fields plus a trailing `,ALERT` (six fields; a strict CSV reader sees a ragged row). File is appended to; header written only for a new/empty file; flushed per line.

## Generic CSV (ours, verified by round-trip tests)
`frequency_mhz,level_dbm` header, `470.000000,-100.5` (6 decimals MHz = 1 Hz, 1 decimal dBm). Detected carriers:
`frequency_mhz,level_dbm,channel` (channel empty outside the plan; all carriers, not capped at 32).

## Importers (`io/importers.py`)
`parse_any` (WSM export when its column header is found, else generic), `parse_generic_csv` (= `parse_wwb_csv` =
`parse_rfe_csv`), `parse_wsm_csv`, `import_file(path)` (UTF-8/BOM, Latin-1 fallback, label = file stem).
- Delimiter per line: `;` (decimal comma allowed), else tab, else `,`; spaces around fields ignored.
- Header optional: any line whose first two fields are not finite numbers is skipped (metadata, header, footer, blank).
- Unit autodetect from the **largest** frequency: `< 10 000` MHz, `< 10 000 000` kHz, else Hz. Ambiguity: a kHz file
  spanning only up to 9.999 MHz is read as MHz (pass `unit=` to force). Result is sorted, duplicates dropped (first wins).
- `470000;;-106` (empty middle field, level in the third) is accepted (WSM-like simple form).
- Exports skip non-finite points. No data -> `ValueError("No scan data found ...")`.

## Shure Wireless Workbench CSV (`wwb_csv`) - confidence: medium
Source: Shure WWB7 manual, "Scanning" / "Upload Scans" pages (content-files.shure.com/Pubs/WWB): `.csv`, `.txt` or
`.spa`; **no header information**; frequency then level separated by comma (comma, semicolon or tab per other
Shure text); minimum step 25 kHz; example `470.000, -109.0` (MHz, dBm). A third-party converter
(github.com/nikharju/WSM-WWB-csv-converter) writes exactly `470.000, -109` lines. We write `MHz(3 decimals), dBm(1 decimal)`
and decimate scans finer than 25 kHz to a 25 kHz grid by taking the **maximum** per bin (peaks survive; frequency =
bin start). Unknown: exact tolerance for non-25 kHz-multiple grids; not tried in a real WWB (plan Q4 stays open).

## Sennheiser WSM CSV (`wsm_csv`, `parse_wsm_csv`) - confidence: low-medium
Sources: the WSM-to-WWB converter above reads a WSM scan export as: `;`-delimited, six preamble lines, then the
column row `Frequency;RF level (%);RF level;Memory (%);Memory;Squelch (%);Squelch` (line 7), then rows with the
frequency in **kHz** (`470000`) and RF level as a percentage; it converts `dBm = pct / 100 * 120 - 120`, so the
scale is 0 % = -120 dBm, 100 % = 0 dBm. The WSM manual / Soundbase docs mention a simpler `470000;;-106` form.
We write: six preamble lines (text is our own guess), the column row, then `kHz;pct(1 decimal);dBm(1 decimal);0;0;0;0`,
decimated to >= 25 kHz like WWB. Unknown: the real content of the preamble, of the `RF level`, `Memory` and `Squelch`
columns, and the footer (the converter drops 13 trailing lines of a full-band export; we skip non-numeric rows).
Real WSM exports are likely integer percent (1.2 dB steps); ours keep one decimal. Not tried in a real WSM.

## RF Explorer for Windows CSV (import) - confidence: medium for Single Signal, none for Cumulative
Source: j3.rf-explorer.com "RF Explorer file formats". "Export Single Signal CSV" = one `MHz, dBm` pair per line (the
format other coordination tools accept); the optional "CSV Header" setting adds metadata lines (Receiver, Date/Time,
RFUnit, Owner, ScanCountry, CRC); both are handled by the tolerant generic parser (metadata lines are skipped).
"Export Cumulative CSV" (all sweeps, start/step in a header, rows of amplitudes per sweep) is **not supported**:
its exact layout is not documented on the web (the vendor says to read their source), so such a file ends in the
"No scan data" error or a wrong trace; the message suggests Single Signal CSV. Unverified against a real file.

## PNG
`io/png.py` `encode_png(rgba uint8 (h, w, 4))`: signature, IHDR (8-bit RGBA), one zlib IDAT with filter 0, IEND.
Plot capture in the UI: see `ui.md` "Files".
