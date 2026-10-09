"""Plan exporters: CSV, TXT and printable HTML (pure encoders + atomic writers)."""

from __future__ import annotations

import base64
import csv
import io
import re
import types
from pathlib import Path

import numpy as np

from opencoord.coord.solver import (
    REASON_TEXT,
    Assignment,
    Plan,
    SolveStats,
    Unassigned,
    Violation,
)
from opencoord.io import export_plan
from opencoord.io.export_plan import CSV_HEADER, PlanDocument
from opencoord.io.png import encode_png

MHZ = 1_000_000
KHZ = 1000


def make_plan(*, warnings: tuple[str, ...] = (), complete: bool = False) -> Plan:
    blocker = Violation("im3_2tx", 100 * KHZ, 25 * KHZ, ("Mic #1", "Mic #2"), None, 600_550_000)
    return Plan(
        assignments=(
            Assignment("Mic #1", "Mic", 600_125_000, None, -98.25, 412_500),
            Assignment("Mic #2", "Mic", 600_500_000, None, None, None),
            Assignment("IEM <A> #1", "IEM <A>", 650_000_000, "Bank 1", -101.0, 1_250_000),
        ),
        unassigned=() if complete else (Unassigned("Mic #3", "Mic", "imd-conflicts", blocker),),
        backups=types.MappingProxyType({"Mic": (601_000_000, 602_250_000), "IEM <A>": ()}),
        warnings=warnings,
        stats=SolveStats(elapsed_s=0.042, nodes=17, complete=complete, timed_out=False),
    )


def doc(plan: Plan | None = None) -> PlanDocument:
    return PlanDocument(
        plan=plan or make_plan(warnings=("Mic #2 at 700.000 MHz is in a forbidden band: LTE",)),
        locked=((610_000_000, "Venue IEM"),),
        generated="2026-10-09T12:00:00+00:00",
        version="0.1.0",
        scan_label="Max hold",
    )


def test_csv_header_rows_backups_and_warnings() -> None:
    text = export_plan.plan_csv(doc())
    assert text.endswith("\n") and "\r" not in text
    lines = text.splitlines()
    assert (
        lines[0] == CSV_HEADER == "device,profile,frequency_mhz,group,scan_level_dbm,imd_margin_khz"
    )
    rows = list(csv.reader(io.StringIO(text)))[1:]
    assert rows[0] == ["Mic #1", "Mic", "600.125", "", "-98.2", "412.5"]
    assert rows[1] == ["Mic #2", "Mic", "600.500", "", "", ""]
    assert rows[2] == ["IEM <A> #1", "IEM <A>", "650.000", "Bank 1", "-101.0", "1250.0"]
    assert ["Mic #3", "Mic", "", "", "", ""] in rows
    assert ["Venue IEM", "locked", "610.000", "", "", ""] in rows
    backups = [r for r in rows if r[0] == "backup"]
    assert backups == [
        ["backup", "Mic", "601.000", "", "", ""],
        ["backup", "Mic", "602.250", "", "", ""],
    ]
    warnings = [r[1] for r in rows if r[0] == "warning"]
    # Plan.warnings are always printed, plus the partial result and why each device is missing.
    assert "Mic #2 at 700.000 MHz is in a forbidden band: LTE" in warnings
    assert any(w.startswith("Partial plan: 3 of 4 devices") for w in warnings)
    assert any(
        w.startswith("Mic #3 (Mic): no frequency - " + REASON_TEXT["imd-conflicts"])
        for w in warnings
    )
    assert all(len(r) == 6 for r in rows)


def test_csv_quotes_commas_in_names() -> None:
    plan = Plan(
        (Assignment("A, B #1", "A, B", 470_000_000),),
        (),
        types.MappingProxyType({}),
        ("x, y",),
        SolveStats(0.0, 1, True, False),
    )
    rows = list(csv.reader(io.StringIO(export_plan.plan_csv(doc(plan)))))
    assert rows[1][:3] == ["A, B #1", "A, B", "470.000"]
    assert ["warning", "x, y", "", "", "", ""] in rows


def test_txt_has_warnings_first_and_aligned_columns() -> None:
    text = export_plan.plan_txt(doc())
    lines = text.splitlines()
    assert lines[0] == "OpenCoord frequency plan"
    assert "Generated 2026-10-09T12:00:00+00:00 by OpenCoord 0.1.0" in text
    warn_at = text.index("WARNINGS")
    table_at = text.index("Device")
    assert warn_at < table_at
    assert "in a forbidden band" in text[warn_at:table_at]
    assert "Partial plan: 3 of 4 devices" in text[warn_at:table_at]
    rows = [ln for ln in lines if ln.startswith(("Mic #1 ", "Mic #2 ", "IEM <A> #1 "))]
    assert len(rows) == 3
    starts = {re.search(r"\d{3}\.\d{3}", ln).start() for ln in rows}  # type: ignore[union-attr]
    assert len(starts) == 1  # the frequencies line up
    assert "Mic #3" in text and REASON_TEXT["imd-conflicts"] in text
    assert re.search(r"Mic\s+601\.000, 602\.250", text)
    assert "Venue IEM" in text and "610.000" in text
    assert "Scan: Max hold" in text


def test_txt_without_warnings_says_complete() -> None:
    text = export_plan.plan_txt(doc(make_plan(complete=True)))
    assert "WARNINGS" not in text
    assert "Complete: all 3 devices have a frequency" in text


def test_html_is_self_contained_escaped_and_embeds_the_png() -> None:
    rgba = np.zeros((2, 3, 4), dtype=np.uint8)
    png = encode_png(rgba)
    html = export_plan.plan_html(doc(), png)
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html and "<link" not in html and "<script" not in html
    assert "IEM &lt;A&gt; #1" in html and "IEM <A>" not in html
    m = re.search(r'src="data:image/png;base64,([^"]+)"', html)
    assert m is not None and base64.b64decode(m.group(1)) == png
    warn = html.index('class="warnings"')
    assert warn < html.index("<table")
    assert "in a forbidden band" in html[warn:]
    assert "2026-10-09T12:00:00+00:00" in html and "OpenCoord 0.1.0" in html
    assert "600.125" in html and "601.000" in html and "Venue IEM" in html


def test_html_without_image() -> None:
    html = export_plan.plan_html(doc(make_plan(complete=True)), None)
    assert "data:image/png" not in html
    assert 'class="warnings"' not in html


def test_writers_are_atomic_utf8(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "plan.txt"
    export_plan.write_text(p, "Ä\n")
    assert p.read_bytes() == "Ä\n".encode()
    assert [x.name for x in p.parent.iterdir()] == ["plan.txt"]


def test_csv_text_cells_cannot_run_formulas() -> None:
    plan = Plan(
        (Assignment("=1+1", "@SUM(A1)", 600_000_000, "+g", -98.2, 1000),),
        (Unassigned("-x #2", "@SUM(A1)", "imd-conflicts"),),
        types.MappingProxyType({"@SUM(A1)": (601_000_000,)}),
        ("=HYPERLINK(1)", "\tTab", "\rCR"),
        SolveStats(0.0, 1, False, False),
    )
    d = PlanDocument(plan, ((610_000_000, "=cmd"),), "t", "v")
    rows = list(csv.reader(io.StringIO(export_plan.plan_csv(d))))
    assert rows[1] == ["'=1+1", "'@SUM(A1)", "600.000", "'+g", "-98.2", "1.0"]
    assert ["'-x #2", "'@SUM(A1)", "", "", "", ""] in rows
    assert ["'=cmd", "locked", "610.000", "", "", ""] in rows
    assert ["backup", "'@SUM(A1)", "601.000", "", "", ""] in rows
    warnings = [r[1] for r in rows if r[0] == "warning"]
    assert "'=HYPERLINK(1)" in warnings and "'\tTab" in warnings and "'\rCR" in warnings
    # Every text cell is guarded; numbers (negative levels) stay as they are.
    for row in rows[1:]:
        assert not any(c[:1] in ("=", "+", "@", "\t", "\r") for c in row)


def test_spreadsheet_safe() -> None:
    from opencoord.io.export_scan import spreadsheet_safe

    assert spreadsheet_safe("=1+1") == "'=1+1"
    assert spreadsheet_safe("-5") == "'-5"
    assert spreadsheet_safe("Mic #1") == "Mic #1" and spreadsheet_safe("") == ""
