"""Frequency plan exporters (pure: plan in, ``str`` out; ``write_text`` does the file I/O).

Formats (details in the skill's ``io-formats.md``):

* CSV: ``device,profile,frequency_mhz,group,scan_level_dbm,imd_margin_khz``; one row per assigned
  device, then unassigned devices (empty frequency), locked carriers (profile ``locked``),
  backups (device ``backup``) and warnings (device ``warning``, the text in the profile column).
  Text cells starting like a formula (``= + - @``, tab, CR) get a leading ``'``.
* TXT: a human-readable sheet with the warnings at the top and aligned columns.
* HTML: a printable, self-contained page (inline CSS, the spectrum as an embedded PNG), with the
  warnings in a highlighted box above the tables.

Every format prints :func:`warning_lines`: ``Plan.warnings`` (forbidden bands, locked clashes)
plus a partial-plan notice and the reason for each device without a frequency.
"""

from __future__ import annotations

import base64
import csv
import html
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from opencoord.coord.solver import Assignment, Plan, describe_unassigned
from opencoord.io.atomic import write_atomic
from opencoord.io.export_scan import spreadsheet_safe

CSV_HEADER: Final = "device,profile,frequency_mhz,group,scan_level_dbm,imd_margin_khz"
TITLE: Final = "OpenCoord frequency plan"


@dataclass(frozen=True)
class PlanDocument:
    """What an export shows: the plan plus its context."""

    plan: Plan
    #: Locked carriers the plan was made around: ``(freq_hz, label)``.
    locked: Sequence[tuple[int, str]]
    #: When the export was made (ISO 8601) and the OpenCoord version.
    generated: str
    version: str
    #: Label of the scan trace used (``None`` = coordinated without a scan).
    scan_label: str | None = None


# --- shared text --------------------------------------------------------------------------------


def mhz(hz: int) -> str:
    return f"{hz / 1e6:.3f}"


def _level(a: Assignment) -> str:
    return "" if a.scan_level_dbm is None else f"{a.scan_level_dbm:.1f}"


def _margin(a: Assignment) -> str:
    return "" if a.nearest_imd_margin_hz is None else f"{a.nearest_imd_margin_hz / 1e3:.1f}"


def device_count(plan: Plan) -> int:
    return len(plan.assignments) + len(plan.unassigned)


def status_line(plan: Plan) -> str:
    """``Complete: ...`` or ``Partial plan: ...`` (the latter is also a warning)."""
    total = device_count(plan)
    if not plan.unassigned:
        return f"Complete: all {total} devices have a frequency"
    text = f"Partial plan: {len(plan.assignments)} of {total} devices have a frequency"
    return text + " (the time budget ran out)" if plan.stats.timed_out else text


def warning_lines(plan: Plan) -> list[str]:
    """Everything an export must show as a warning, most important first."""
    lines = [] if not plan.unassigned else [status_line(plan)]
    lines += plan.warnings
    lines += [
        f"{u.label} ({u.profile_name}): no frequency - {describe_unassigned(u)}"
        for u in plan.unassigned
    ]
    return lines


def search_line(plan: Plan) -> str:
    return f"Search: {plan.stats.nodes} nodes in {plan.stats.elapsed_s:.2f} s"


def _scan_text(document: PlanDocument) -> str:
    return f"Scan: {document.scan_label}" if document.scan_label else "Scan: not used"


def _generated_text(document: PlanDocument) -> str:
    return f"Generated {document.generated} by OpenCoord {document.version}"


def _table_rows(plan: Plan) -> list[tuple[str, str, str, str, str, str]]:
    return [
        (a.label, a.profile_name, mhz(a.freq_hz), a.group or "", _level(a), _margin(a))
        for a in plan.assignments
    ]


# --- CSV ----------------------------------------------------------------------------------------


def plan_csv(document: PlanDocument) -> str:
    """Text cells (device, profile, group, warnings) go through ``spreadsheet_safe`` so a name
    like ``=HYPERLINK(...)`` cannot run as a formula; number cells are written as they are."""
    plan = document.plan
    t = spreadsheet_safe
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    out.writerow(CSV_HEADER.split(","))
    out.writerows(
        [t(label), t(profile), freq, t(group), level, margin]
        for label, profile, freq, group, level, margin in _table_rows(plan)
    )
    out.writerows([t(u.label), t(u.profile_name), "", "", "", ""] for u in plan.unassigned)
    out.writerows([t(label), "locked", mhz(f), "", "", ""] for f, label in document.locked)
    for name, freqs in plan.backups.items():
        out.writerows(["backup", t(name), mhz(f), "", "", ""] for f in freqs)
    out.writerows(["warning", t(w), "", "", "", ""] for w in warning_lines(plan))
    return buf.getvalue()


# --- TXT ----------------------------------------------------------------------------------------

_TXT_HEADER: Final = (
    "Device",
    "Profile",
    "Frequency (MHz)",
    "Group",
    "Scan (dBm)",
    "IMD margin (kHz)",
)
#: Columns aligned right (numbers).
_RIGHT: Final = (False, False, True, False, True, True)


def _aligned(rows: Sequence[Sequence[str]], right: Sequence[bool]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [
            c.rjust(w) if align else c.ljust(w)
            for c, w, align in zip(r, widths, right, strict=True)
        ]
        out.append("  ".join(cells).rstrip())
    return out


def plan_txt(document: PlanDocument) -> str:
    plan = document.plan
    lines = [TITLE, _generated_text(document), _scan_text(document), ""]
    warnings = warning_lines(plan)
    if warnings:
        lines.append("WARNINGS")
        lines += [f"  ! {w}" for w in warnings]
        lines.append("")
    else:
        lines.append(status_line(plan))
    lines += [search_line(plan), ""]
    if plan.assignments:
        rows = [_TXT_HEADER, *(tuple(c or "-" for c in r) for r in _table_rows(plan))]
        table = _aligned(rows, _RIGHT)
        lines += [table[0], "-" * max(len(t) for t in table), *table[1:], ""]
    else:
        lines += ["No device has a frequency.", ""]
    if plan.unassigned:
        lines.append("Unassigned")
        lines += [
            f"  {u.label} ({u.profile_name}): {describe_unassigned(u)}" for u in plan.unassigned
        ]
        lines.append("")
    if plan.backups:
        lines.append("Backups (MHz)")
        names = list(plan.backups)
        width = max(len(n) for n in names)
        for name in names:
            freqs = ", ".join(mhz(f) for f in plan.backups[name]) or "-"
            lines.append(f"  {name.ljust(width)}  {freqs}")
        lines.append("")
    if document.locked:
        lines.append("Locked carriers (MHz)")
        width = max(len(label) for _, label in document.locked)
        lines += [f"  {label.ljust(width)}  {mhz(f)}" for f, label in document.locked]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# --- HTML ---------------------------------------------------------------------------------------

_CSS: Final = """
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #111;
  margin: 24px; font-size: 13px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 18px 0 6px; }
.meta { color: #555; margin: 0 0 2px; }
.warnings { border: 2px solid #c0392b; background: #fdecea; color: #7b1d13; padding: 8px 12px;
  margin: 12px 0; border-radius: 4px; }
.warnings h2 { margin: 0 0 4px; color: #c0392b; }
.warnings ul { margin: 0; padding-left: 18px; }
.ok { color: #1e7a4f; font-weight: 600; }
table { border-collapse: collapse; margin: 4px 0 8px; }
th, td { border: 1px solid #bbb; padding: 3px 8px; text-align: left; }
th { background: #eee; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
img { max-width: 100%; border: 1px solid #bbb; margin-top: 8px; }
@media print { body { margin: 10mm; } .warnings, tr { break-inside: avoid; } }
""".strip()


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _html_table(header: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[bool]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in header)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{_e(c)}</td>' if r else f"<td>{_e(c)}</td>"
            for c, r in zip(row, right, strict=True)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def plan_html(document: PlanDocument, png: bytes | None = None) -> str:
    """A printable page; ``png`` (the spectrum picture) is embedded as a data URI."""
    plan = document.plan
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{_e(TITLE)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_e(TITLE)}</h1>",
        f'<p class="meta">{_e(_generated_text(document))}</p>',
        f'<p class="meta">{_e(_scan_text(document))}; {_e(search_line(plan))}</p>',
    ]
    warnings = warning_lines(plan)
    if warnings:
        items = "".join(f"<li>{_e(w)}</li>" for w in warnings)
        parts.append(f'<div class="warnings"><h2>Warnings</h2><ul>{items}</ul></div>')
    else:
        parts.append(f'<p class="ok">{_e(status_line(plan))}</p>')
    parts.append("<h2>Frequencies</h2>")
    if plan.assignments:
        rows = [tuple(c or "-" for c in r) for r in _table_rows(plan)]
        parts.append(_html_table(_TXT_HEADER, rows, _RIGHT))
    else:
        parts.append("<p>No device has a frequency.</p>")
    if plan.unassigned:
        parts.append("<h2>Unassigned</h2>")
        rows = [(u.label, u.profile_name, describe_unassigned(u)) for u in plan.unassigned]
        parts.append(_html_table(("Device", "Profile", "Reason"), rows, (False, False, False)))
    if plan.backups:
        parts.append("<h2>Backups</h2>")
        rows = [
            (name, ", ".join(mhz(f) for f in freqs) or "-") for name, freqs in plan.backups.items()
        ]
        parts.append(_html_table(("Profile", "Frequencies (MHz)"), rows, (False, False)))
    if document.locked:
        parts.append("<h2>Locked carriers</h2>")
        rows = [(label, mhz(f)) for f, label in document.locked]
        parts.append(_html_table(("Label", "Frequency (MHz)"), rows, (False, True)))
    if png is not None:
        data = base64.b64encode(png).decode("ascii")
        parts.append("<h2>Spectrum</h2>")
        parts.append(f'<img alt="Spectrum with the plan" src="data:image/png;base64,{data}">')
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


# --- file I/O -----------------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 (``\\n`` line endings) atomically."""
    write_atomic(path, text.encode("utf-8"))


__all__ = [
    "CSV_HEADER",
    "TITLE",
    "PlanDocument",
    "device_count",
    "mhz",
    "plan_csv",
    "plan_html",
    "plan_txt",
    "search_line",
    "status_line",
    "warning_lines",
    "write_text",
]
