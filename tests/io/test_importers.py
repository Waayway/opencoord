from pathlib import Path

import numpy as np
import pytest

from opencoord.io import importers

MHZ = 1_000_000


def test_generic_mhz_with_header() -> None:
    t = importers.parse_generic_csv(
        "frequency_mhz,level_dbm\n470.000000,-100.5\n470.025000,-99.0\n", "x"
    )
    assert t.label == "x"
    assert t.freqs_hz.tolist() == [470 * MHZ, 470_025_000]
    assert t.dbm.dtype == np.float32 and t.freqs_hz.dtype == np.float64
    assert t.dbm.tolist() == [-100.5, -99.0]


@pytest.mark.parametrize("sep", [",", ";", "\t", ", "])
def test_delimiters(sep: str) -> None:
    t = importers.parse_generic_csv(f"470.0{sep}-100\n471.0{sep}-90\n", "x")
    assert t.freqs_hz.tolist() == [470 * MHZ, 471 * MHZ]


def test_decimal_comma_with_semicolon() -> None:
    t = importers.parse_generic_csv("470,5;-100,5\n471,0;-90,0\n", "x")
    assert t.freqs_hz.tolist() == [470_500_000, 471 * MHZ]
    assert t.dbm.tolist() == [-100.5, -90.0]


def test_unit_detection_by_magnitude() -> None:
    mhz = importers.parse_generic_csv("470.0,-1\n960.0,-2\n", "x")
    khz = importers.parse_generic_csv("470000,-1\n960000,-2\n", "x")
    hz = importers.parse_generic_csv("470000000,-1\n960000000,-2\n", "x")
    for t in (mhz, khz, hz):
        assert t.freqs_hz.tolist() == [470 * MHZ, 960 * MHZ]


def test_explicit_unit() -> None:
    t = importers.parse_generic_csv("5,-1\n6,-2\n", "x", unit="mhz")
    assert t.freqs_hz.tolist() == [5 * MHZ, 6 * MHZ]


def test_sorted_and_deduplicated_and_crlf_bom() -> None:
    t = importers.parse_generic_csv("﻿471.0,-90\r\n470.0,-100\r\n470.0,-80\r\n", "x")
    assert t.freqs_hz.tolist() == [470 * MHZ, 471 * MHZ]
    assert t.dbm.tolist() == [-100.0, -90.0]


def test_rf_explorer_with_metadata_lines() -> None:
    text = (
        "RF Explorer Single Signal CSV\n"
        "Receiver: RF Explorer WSUB1G+\n"
        "Date/Time: 2020-01-01\n"
        "\n"
        "470.000, -100.0\n"
        "470.100, -90.0\n"
    )
    t = importers.parse_rfe_csv(text, "rfe")
    assert t.freqs_hz.tolist() == [470 * MHZ, 470_100_000]


def test_wwb_headerless() -> None:
    t = importers.parse_wwb_csv("470.000, -109.0\n470.025, -108.0\n", "w")
    assert t.dbm.tolist() == [-109.0, -108.0]


WSM = (
    "Sennheiser WSM\nline2\nline3\nline4\nline5\nline6\n"
    "Frequency;RF level (%);RF level;Memory (%);Memory;Squelch (%);Squelch\n"
    "470000;10;-108;0;0;0;0\n"
    "470025;50;-60;0;0;0;0\n"
    "footer;;;\n"
)


def test_wsm_export() -> None:
    t = importers.parse_wsm_csv(WSM, "s")
    assert t.freqs_hz.tolist() == [470 * MHZ, 470_025_000]
    assert t.dbm.tolist() == pytest.approx([-108.0, -60.0])
    assert importers.parse_any(WSM, "s").freqs_hz.tolist() == t.freqs_hz.tolist()


def test_wsm_simple_form() -> None:
    t = importers.parse_any("470000;;-106\n470025;;-100\n", "s")
    assert t.freqs_hz.tolist() == [470 * MHZ, 470_025_000]
    assert t.dbm.tolist() == [-106.0, -100.0]


@pytest.mark.parametrize("text", ["", "hello\nworld\n", "frequency_mhz,level_dbm\n"])
def test_no_data_is_an_error(text: str) -> None:
    with pytest.raises(ValueError, match="No scan data"):
        importers.parse_any(text, "x")


def test_non_finite_lines_are_skipped() -> None:
    t = importers.parse_generic_csv("470.0,nan\ninf,-3\n471.0,-1\n", "x")
    assert t.freqs_hz.tolist() == [471 * MHZ]


def test_import_file(tmp_path: Path) -> None:
    p = tmp_path / "scan.csv"
    p.write_bytes("470.0,-100\n471.0,-90\n".encode("latin-1"))
    t = importers.import_file(p)
    assert t.label == "scan"
    assert len(t.freqs_hz) == 2
