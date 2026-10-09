"""Scan exporters (pure: trace in, ``str``/``bytes`` out; ``write_*`` does the file I/O).

Formats (details and sources in the skill's ``io-formats.md``):

* generic CSV: ``frequency_mhz,level_dbm`` header, 6 decimals MHz, 1 decimal dBm, ``\\n``.
* WWB CSV: Shure Wireless Workbench import, no header, ``470.000, -109.0``, >= 25 kHz step.
* WSM CSV: Sennheiser Wireless Systems Manager scan layout (semicolons, kHz, level in %).
* detected carriers CSV: ``frequency_mhz,level_dbm,channel``.
* PNG of the plot from an RGBA pixel array.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

from opencoord.core.types import Carrier, Trace
from opencoord.io.atomic import write_atomic
from opencoord.io.png import encode_png

GENERIC_HEADER: Final = "frequency_mhz,level_dbm"
CARRIERS_HEADER: Final = "frequency_mhz,level_dbm,channel"
#: WWB rejects scans with a step below 25 kHz.
WWB_MIN_STEP_HZ: Final = 25_000
#: WSM shows RF level as a percentage of this range: ``pct = (dBm + 120) / 120 * 100``.
WSM_FLOOR_DBM: Final = -120.0
WSM_RANGE_DB: Final = 120.0
WSM_HEADER_COLUMNS: Final = "Frequency;RF level (%);RF level;Memory (%);Memory;Squelch (%);Squelch"
#: WSM exports six preamble lines before the column header.
WSM_PREAMBLE_LINES: Final = 6


#: First characters that make a spreadsheet treat a cell as a formula (OWASP CSV injection).
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(text: str) -> str:
    """Free text for a CSV cell: prefixed with ``'`` when it starts like a formula, so Excel /
    LibreOffice show it instead of running it. Only for text cells, never for numbers."""
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _clean(trace: Trace) -> Trace:
    """``trace`` minus non-finite points (skipped); ``ValueError`` if none remain."""
    if len(trace.freqs_hz) != len(trace.dbm):
        raise ValueError("The trace has no data to export")
    ok = np.isfinite(trace.freqs_hz) & np.isfinite(trace.dbm)
    if not ok.any():
        raise ValueError("The trace has no data to export")
    if ok.all():
        return trace
    return Trace(trace.freqs_hz[ok], trace.dbm[ok], trace.label)


def _mhz(hz: float, decimals: int = 6) -> str:
    return f"{hz / 1e6:.{decimals}f}"


def generic_csv(trace: Trace) -> str:
    trace = _clean(trace)
    lines = [GENERIC_HEADER]
    lines += [
        f"{_mhz(f)},{d:.1f}"
        for f, d in zip(trace.freqs_hz.tolist(), trace.dbm.tolist(), strict=True)
    ]
    return "\n".join(lines) + "\n"


def _decimate(
    trace: Trace, step_hz: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float32]]:
    """Max of the points in each ``step_hz`` bin (anchored at the first point), at the bin start.

    Traces already at least ``step_hz`` apart are returned unchanged.
    """
    freqs, dbm = trace.freqs_hz, trace.dbm
    if len(freqs) < 2 or float(np.min(np.diff(freqs))) >= step_hz:
        return freqs, dbm
    bins = np.floor((freqs - freqs[0]) / step_hz + 1e-9).astype(np.int64)
    starts = np.flatnonzero(np.r_[True, np.diff(bins) > 0])
    return freqs[0] + bins[starts] * float(step_hz), np.maximum.reduceat(dbm, starts)


def wwb_csv(trace: Trace) -> str:
    """Shure WWB scan import: ``470.000, -109.0`` lines, no header, >= 25 kHz step."""
    trace = _clean(trace)
    freqs, dbm = _decimate(trace, WWB_MIN_STEP_HZ)
    lines = [f"{_mhz(f, 3)}, {d:.1f}" for f, d in zip(freqs.tolist(), dbm.tolist(), strict=True)]
    return "\n".join(lines) + "\n"


def wsm_csv(trace: Trace) -> str:
    """Sennheiser WSM layout: preamble, column header, ``kHz;RF level (%);dBm;0;0;0;0``."""
    trace = _clean(trace)
    freqs, dbm = _decimate(trace, WWB_MIN_STEP_HZ)
    lines = [
        "OpenCoord scan export",
        f"Label;{spreadsheet_safe(trace.label)}",
        f"Start (kHz);{round(float(freqs[0]) / 1e3)}",
        f"Stop (kHz);{round(float(freqs[-1]) / 1e3)}",
        f"Points;{len(freqs)}",
        "",
        WSM_HEADER_COLUMNS,
    ]
    pct = np.clip((dbm.astype(np.float64) - WSM_FLOOR_DBM) / WSM_RANGE_DB * 100.0, 0.0, 100.0)
    for f, p, d in zip(freqs.tolist(), pct.tolist(), dbm.tolist(), strict=True):
        lines.append(f"{round(f / 1e3)};{p:.1f};{max(d, WSM_FLOOR_DBM):.1f};0;0;0;0")
    return "\n".join(lines) + "\n"


def carriers_csv(rows: Sequence[tuple[Carrier, int | None]]) -> str:
    """``rows`` = (carrier, channel number or ``None`` outside the plan)."""
    lines = [CARRIERS_HEADER]
    for carrier, channel in rows:
        ch = "" if channel is None else str(channel)
        lines.append(f"{_mhz(carrier.freq_hz)},{carrier.level_dbm:.1f},{ch}")
    return "\n".join(lines) + "\n"


def png_bytes(rgba: npt.NDArray[np.uint8]) -> bytes:
    return encode_png(rgba)


def write_text(path: Path, text: str) -> None:
    write_atomic(path, text.encode("utf-8"))


def write_bytes(path: Path, data: bytes) -> None:
    write_atomic(path, data)
