"""Long-run logger: max-hold rows per interval, threshold alerts with debounce, CSV writer."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from opencoord.core.logger import (
    CSV_HEADER,
    LoggerEngine,
    LogRange,
    LogWriter,
    alert_csv,
    row_csv,
)

MHZ = 1_000_000
FREQS = np.arange(470 * MHZ, 480 * MHZ + 1, MHZ, dtype=np.float64)  # 470..480 MHz, 11 bins


def levels(**peaks: float) -> np.ndarray:
    """-100 dBm everywhere with ``f475=-60`` style overrides (MHz in the keyword)."""
    dbm = np.full(len(FREQS), -100.0, dtype=np.float32)
    for name, level in peaks.items():
        dbm[int(name[1:]) - 470] = level
    return dbm


def engine(**kw: object) -> LoggerEngine:
    e = LoggerEngine(
        kw.pop("ranges", [LogRange(470 * MHZ, 480 * MHZ)]),  # type: ignore[arg-type]
        interval_s=kw.pop("interval_s", 60.0),  # type: ignore[arg-type]
        threshold_dbm=kw.pop("threshold_dbm", None),  # type: ignore[arg-type]
    )
    assert not kw
    e.start(0.0)
    return e


def test_no_row_before_the_interval_and_none_without_data() -> None:
    e = engine()
    assert not e.due(59.9)
    assert e.due(60.0)
    assert e.take_rows(60.0, "2026-10-09T10:00:00+00:00") == []  # nothing was fed
    assert not e.due(60.1)  # the interval restarted


def test_row_is_the_max_hold_since_the_last_write() -> None:
    e = engine()
    e.feed(FREQS, levels(f472=-70.0, f475=-80.0), 1.0)
    e.feed(FREQS, levels(f474=-65.0), 30.0)
    (row,) = e.take_rows(60.0, "T1")
    assert (row.timestamp_iso, row.start_hz, row.stop_hz) == ("T1", 470 * MHZ, 480 * MHZ)
    assert row.max_dbm == pytest.approx(-65.0) and row.peak_hz == 474 * MHZ
    # Max hold restarts after each write.
    e.feed(FREQS, levels(f478=-90.0), 61.0)
    assert e.due(120.0)
    (row2,) = e.take_rows(120.0, "T2")
    assert row2.max_dbm == pytest.approx(-90.0) and row2.peak_hz == 478 * MHZ


def test_one_row_per_range_and_only_bins_inside_count() -> None:
    e = engine(ranges=[LogRange(471 * MHZ, 473 * MHZ), LogRange(477 * MHZ, 480 * MHZ)])
    e.feed(FREQS, levels(f470=-30.0, f472=-70.0, f480=-60.0), 1.0)
    rows = e.take_rows(60.0, "T")
    assert [(r.start_hz, r.max_dbm, r.peak_hz) for r in rows] == [
        (471 * MHZ, pytest.approx(-70.0), 472 * MHZ),
        (477 * MHZ, pytest.approx(-60.0), 480 * MHZ),  # the range edges are inclusive
    ]


def test_range_outside_the_sweep_gets_no_row() -> None:
    e = engine(ranges=[LogRange(470 * MHZ, 475 * MHZ), LogRange(800 * MHZ, 810 * MHZ)])
    e.feed(FREQS, levels(), 1.0)
    rows = e.take_rows(60.0, "T")
    assert [r.start_hz for r in rows] == [470 * MHZ]


def test_threshold_alert_when_any_bin_exceeds() -> None:
    e = engine(threshold_dbm=-75.0)
    assert e.feed(FREQS, levels(f475=-75.0), 1.0) == []  # equal is not "exceeds"
    (alert,) = e.feed(FREQS, levels(f476=-60.0, f473=-70.0), 2.0)
    assert alert.level_dbm == pytest.approx(-60.0) and alert.peak_hz == 476 * MHZ
    assert (alert.start_hz, alert.stop_hz, alert.threshold_dbm) == (470 * MHZ, 480 * MHZ, -75.0)


def test_no_alert_without_a_threshold() -> None:
    e = engine(threshold_dbm=None)
    assert e.feed(FREQS, levels(f476=-10.0), 1.0) == []
    e.set_threshold(-50.0)
    assert len(e.feed(FREQS, levels(f476=-10.0), 2.0)) == 1


def test_alert_debounce_is_per_range_and_one_interval() -> None:
    e = engine(
        ranges=[LogRange(470 * MHZ, 474 * MHZ), LogRange(476 * MHZ, 480 * MHZ)],
        threshold_dbm=-75.0,
        interval_s=10.0,
    )
    a = e.feed(FREQS, levels(f472=-60.0), 1.0)
    assert [x.start_hz for x in a] == [470 * MHZ]
    assert e.feed(FREQS, levels(f472=-60.0), 5.0) == []  # same range, within the interval
    b = e.feed(FREQS, levels(f472=-60.0, f478=-60.0), 6.0)
    assert [x.start_hz for x in b] == [476 * MHZ]  # the other range is independent
    assert [x.start_hz for x in e.feed(FREQS, levels(f472=-60.0), 11.0)] == [470 * MHZ]
    # Debounce does not hide the level from the row.
    (r1, r2) = e.take_rows(11.0, "T")
    assert r1.max_dbm == pytest.approx(-60.0) and r2.max_dbm == pytest.approx(-60.0)


def test_configuration_is_validated() -> None:
    with pytest.raises(ValueError, match="interval"):
        LoggerEngine([LogRange(1, 2)], interval_s=0.5)
    with pytest.raises(ValueError, match="range"):
        LoggerEngine([], interval_s=60.0)
    with pytest.raises(ValueError, match="start"):
        LogRange(5, 5)
    with pytest.raises(ValueError, match="at most"):
        LoggerEngine([LogRange(i, i + 1) for i in range(20)], interval_s=60.0)


def test_csv_lines() -> None:
    e = engine(threshold_dbm=-90.0)
    (alert,) = e.feed(FREQS, levels(f475=-62.34), 1.0)
    (row,) = e.take_rows(60.0, "2026-10-09T10:00:00+00:00")
    assert CSV_HEADER == "timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz,kind"
    assert row_csv(row) == "2026-10-09T10:00:00+00:00,470.000000,480.000000,-62.3,475.000000,DATA"
    assert alert_csv("2026-10-09T10:00:01+00:00", alert) == (
        "2026-10-09T10:00:01+00:00,470.000000,480.000000,-62.3,475.000000,ALERT"
    )


def test_writer_creates_header_once_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "log.csv"
    w = LogWriter(path)
    w.write_line("a,1")
    w.close()
    w2 = LogWriter(path)  # reopening an existing log appends without a second header
    w2.write_line("b,2")
    w2.close()
    assert path.read_text("utf-8").splitlines() == [CSV_HEADER, "a,1", "b,2"]


def test_writer_flushes_every_line(tmp_path: Path) -> None:
    w = LogWriter(tmp_path / "l.csv")
    w.write_line("x,1")
    assert (tmp_path / "l.csv").read_text("utf-8").splitlines()[-1] == "x,1"  # before close()
    w.close()
    w.close()


def test_writer_never_mixes_layouts(tmp_path: Path) -> None:
    old = tmp_path / "log.csv"
    old.write_text("timestamp_iso,range_start_mhz,range_stop_mhz,max_dbm,peak_mhz\nx,1,2,3,4\n")
    w = LogWriter(old)
    assert w.redirected and w.path == tmp_path / "log-1.csv" and w.requested == old
    w.write_line("a")
    w.close()
    assert old.read_text().count("\n") == 2  # untouched
    assert lines_of(tmp_path / "log-1.csv") == [CSV_HEADER, "a"]
    w2 = LogWriter(old)  # the numbered file has the right header now: reused
    assert w2.path == tmp_path / "log-1.csv"
    w2.close()
    (tmp_path / "log-1.csv").write_text("other\n")
    w3 = LogWriter(old)
    assert w3.path == tmp_path / "log-2.csv"
    w3.close()
    assert not LogWriter(tmp_path / "fresh.csv").redirected


def lines_of(path: Path) -> list[str]:
    return path.read_text("utf-8").splitlines()


def test_restart_forgets_the_interval_max_and_debounce() -> None:
    e = engine(threshold_dbm=-75.0, interval_s=10.0)
    assert len(e.feed(FREQS, levels(f475=-60.0), 100.0)) == 1
    assert e.feed(FREQS, levels(f475=-60.0), 101.0) == []  # debounced
    e.restart(5.0)  # the time base went back (a replay was rewound)
    assert len(e.feed(FREQS, levels(f475=-60.0), 5.0)) == 1
    assert not e.due(14.9) and e.due(15.0)
    (row,) = e.take_rows(15.0, "T")
    assert row.max_dbm == pytest.approx(-60.0)
