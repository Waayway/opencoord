"""Scan importers: text in, a Trace out (``import_file`` reads a file).

One tolerant line parser serves the generic, WWB and RF Explorer CSV formats: lines whose first
two fields are numbers are data, every other line (header, metadata, blank, footer) is skipped.
The Sennheiser WSM export is recognised by its column header. See ``io-formats.md`` in the skill.

Frequency unit autodetect (``unit="auto"``), by the **largest** frequency in the file:
``< 10 000`` -> MHz, ``< 10 000 000`` -> kHz, otherwise Hz. So kHz files below 10 MHz of range
(e.g. ``5000`` meaning 5 MHz) are read as MHz; pass an explicit ``unit`` for those.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Final, Literal

import numpy as np

from opencoord.core.types import Trace

Unit = Literal["auto", "mhz", "khz", "hz"]

_UNIT_HZ: Final = {"mhz": 1e6, "khz": 1e3, "hz": 1.0}
_MHZ_LIMIT: Final = 10_000.0
_KHZ_LIMIT: Final = 10_000_000.0
_WSM_FIRST_COLUMNS: Final = ("Frequency", "RF level (%)")
#: WSM percentage scale: dBm = pct / 100 * 120 - 120.
_WSM_RANGE_DB: Final = 120.0


def _split(line: str) -> list[str]:
    """Fields; delimiter ``;`` > tab > ``,`` (``;`` files may use a decimal comma)."""
    if ";" in line:
        return [f.strip().replace(",", ".") for f in line.split(";")]
    if "\t" in line:
        return [f.strip() for f in line.split("\t")]
    return [f.strip() for f in line.split(",")]


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _lines(text: str) -> list[str]:
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _make_trace(freqs: list[float], dbm: list[float], label: str, scale: float | None) -> Trace:
    if not freqs:
        raise ValueError(
            "No scan data found (expected lines of frequency and level; "
            "cumulative RF Explorer CSV is not supported, use Export Single Signal CSV)"
        )
    f = np.asarray(freqs, dtype=np.float64)
    if scale is None:
        top = float(np.max(f))
        scale = 1e6 if top < _MHZ_LIMIT else 1e3 if top < _KHZ_LIMIT else 1.0
    hz = np.round(f * scale)
    order = np.argsort(hz, kind="stable")
    hz_sorted = hz[order]
    levels = np.asarray(dbm, dtype=np.float32)[order]
    keep = np.r_[True, np.diff(hz_sorted) > 0]  # duplicate frequencies: first one wins
    return Trace(hz_sorted[keep], levels[keep], label)


def _scale(unit: Unit) -> float | None:
    return None if unit == "auto" else _UNIT_HZ[unit]


def parse_generic_csv(text: str, label: str, *, unit: Unit = "auto") -> Trace:
    """``frequency,level`` lines; delimiter comma, semicolon or tab; header optional."""
    freqs: list[float] = []
    levels: list[float] = []
    for line in _lines(text):
        if not line.strip():
            continue
        fields = _split(line)
        if len(fields) < 2:
            continue
        f = _number(fields[0])
        # WSM-style ``470000;;-106``: empty level percentage column, level in the third field.
        raw = fields[2] if fields[1] == "" and len(fields) >= 3 else fields[1]
        d = _number(raw)
        if f is None or d is None:
            continue
        freqs.append(f)
        levels.append(d)
    return _make_trace(freqs, levels, label, _scale(unit))


#: Shure Wireless Workbench scan import: same two columns, no header.
parse_wwb_csv = parse_generic_csv
#: RF Explorer for Windows "Export Single Signal CSV": MHz,dBm pairs, optional metadata lines.
parse_rfe_csv = parse_generic_csv


def _is_wsm_header(fields: list[str]) -> bool:
    return len(fields) >= 2 and tuple(fields[:2]) == _WSM_FIRST_COLUMNS


def parse_wsm_csv(text: str, label: str) -> Trace:
    """Sennheiser WSM scan export: semicolons, kHz, level in % of -120..0 dBm."""
    freqs: list[float] = []
    levels: list[float] = []
    in_data = False
    for line in _lines(text):
        fields = [f.strip() for f in line.split(";")]
        if not in_data:
            in_data = _is_wsm_header(fields)
            continue
        if len(fields) < 2:
            continue
        f, pct = _number(fields[0]), _number(fields[1].replace(",", "."))
        if f is None or pct is None:
            continue
        freqs.append(f)
        levels.append(pct / 100.0 * _WSM_RANGE_DB - _WSM_RANGE_DB)
    if not in_data:
        raise ValueError("Not a WSM scan export (column header not found)")
    return _make_trace(freqs, levels, label, 1e3)


def parse_any(text: str, label: str) -> Trace:
    """WSM export when its column header is present, otherwise the generic parser."""
    for line in _lines(text)[:40]:
        if _is_wsm_header([f.strip() for f in line.split(";")]):
            return parse_wsm_csv(text, label)
    return parse_generic_csv(text, label)


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM), falling back to Latin-1 for old Windows exports."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def import_file(path: Path) -> Trace:
    """Read a scan file of any supported format; the trace is labelled with the file name."""
    return parse_any(decode_text(path.read_bytes()), path.stem)
