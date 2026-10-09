import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.core.types import Carrier, Trace
from opencoord.io import export_scan, importers

MHZ = 1_000_000


def trace(freqs: list[int], dbm: list[float]) -> Trace:
    return Trace(np.array(freqs, dtype=np.float64), np.array(dbm, dtype=np.float32), "t")


def test_generic_csv_exact_text() -> None:
    t = trace([470 * MHZ, 470_025_001], [-100.04, -99.96])
    assert export_scan.generic_csv(t) == (
        "frequency_mhz,level_dbm\n470.000000,-100.0\n470.025001,-100.0\n"
    )


@given(
    st.lists(st.integers(1_000, 6_000_000_000), min_size=1, max_size=40, unique=True),
    st.data(),
)
def test_generic_csv_round_trip(freqs: list[int], data: st.DataObject) -> None:
    freqs.sort()
    tenths = data.draw(st.lists(st.integers(-1500, 300), min_size=len(freqs), max_size=len(freqs)))
    t = trace(freqs, [v / 10 for v in tenths])
    back = importers.parse_generic_csv(export_scan.generic_csv(t), "t", unit="mhz")
    assert back.freqs_hz.tolist() == freqs
    assert np.allclose(back.dbm, t.dbm, atol=1e-4)
    auto = importers.parse_generic_csv(export_scan.generic_csv(t), "t", unit="mhz")
    assert auto.freqs_hz.tolist() == freqs


def test_wwb_headerless_25khz_grid() -> None:
    f = [470_000_000 + i * 25_000 for i in range(4)]
    text = export_scan.wwb_csv(trace(f, [-100.0, -90.5, -80.0, -70.0]))
    assert text == "470.000, -100.0\n470.025, -90.5\n470.050, -80.0\n470.075, -70.0\n"
    back = importers.parse_wwb_csv(text, "w")
    assert back.freqs_hz.tolist() == f


def test_wwb_decimates_to_25khz_keeping_peaks() -> None:
    f = [470_000_000 + i * 5_000 for i in range(11)]  # 470.000 .. 470.050
    dbm = [-100.0] * 11
    dbm[2] = -50.0  # inside the first 25 kHz bin
    back = importers.parse_wwb_csv(export_scan.wwb_csv(trace(f, dbm)), "w")
    assert back.freqs_hz.tolist() == [470_000_000, 470_025_000, 470_050_000]
    assert back.dbm.tolist() == [-50.0, -100.0, -100.0]


def test_wsm_round_trip() -> None:
    f = [470_000_000 + i * 25_000 for i in range(5)]
    dbm = [-120.0, -108.0, -60.0, -30.0, 0.0]
    text = export_scan.wsm_csv(trace(f, dbm))
    assert "\r" not in text
    back = importers.parse_any(text, "s")
    assert back.freqs_hz.tolist() == f
    assert back.dbm.tolist() == pytest.approx(dbm, abs=0.6)  # 1 % = 1.2 dB steps


def test_wsm_clamps_levels() -> None:
    back = importers.parse_wsm_csv(export_scan.wsm_csv(trace([470 * MHZ], [-150.0])), "s")
    assert back.dbm.tolist() == [-120.0]


def test_carriers_csv() -> None:
    rows = [(Carrier(470_100_000, -50.04), 21), (Carrier(600 * MHZ, -60.0), None)]
    assert export_scan.carriers_csv(rows) == (
        "frequency_mhz,level_dbm,channel\n470.100000,-50.0,21\n600.000000,-60.0,\n"
    )


def test_png_bytes_uses_rgba_array() -> None:
    data = export_scan.png_bytes(np.zeros((3, 5, 4), dtype=np.uint8))
    assert data.startswith(b"\x89PNG")


def test_empty_trace_rejected() -> None:
    with pytest.raises(ValueError):
        export_scan.generic_csv(trace([], []))
