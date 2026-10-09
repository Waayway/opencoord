"""FileActions: sessions, exports and imports against a Controller (no display)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from opencoord.core.types import Sweep
from opencoord.device.scanner import Resolution
from opencoord.device.simulator import SimulatedLink
from opencoord.ui.controller import Controller
from opencoord.ui.files import FileActions, with_suffix

MHZ = 1_000_000


def make() -> tuple[Controller, FileActions]:
    c = Controller(lambda _p: SimulatedLink(seed=1, sweep_interval_s=0.01), simulator=True)
    return c, FileActions(c)


def feed(c: Controller, peak_mhz: float = 600.0) -> None:
    freqs = 590e6 + 25e3 * np.arange(801, dtype=np.float64)
    dbm = np.full(801, -100.0, dtype=np.float32)
    dbm[round((peak_mhz - 590) * 40)] = -40.0
    c.state.traces.update(Sweep(freqs, dbm, 0.0))
    c.state.trace_version += 1


def configure(c: Controller) -> None:
    feed(c)
    c.set_mode("scan")
    c.set_resolution(Resolution.FINE)
    c.set_range(590 * MHZ, 610 * MHZ)
    c.add_marker(600 * MHZ)
    c.add_exclusion_zone(595 * MHZ, 596 * MHZ)
    c.set_threshold_dbm(-85.0)
    c.set_overlay_enabled(True)
    c.freeze_reference()


def test_save_open_round_trip(tmp_path: Path) -> None:
    c1, f1 = make()
    configure(c1)
    p = tmp_path / "show"
    assert f1.save(p)
    assert f1.path == tmp_path / "show.opencoord"

    c2, f2 = make()
    assert f2.open(f1.path)
    a, b = c1.state, c2.state
    assert (b.mode, b.resolution, b.start_hz, b.stop_hz) == (
        "scan",
        Resolution.FINE,
        590 * MHZ,
        610 * MHZ,
    )
    assert b.markers == a.markers and b.exclusion_zones == a.exclusion_zones
    assert b.threshold_dbm == -85.0 and b.overlay_enabled
    assert list(b.references) == ["ref1"]
    assert b.traces.max_hold is not None and a.traces.max_hold is not None
    assert np.array_equal(b.traces.max_hold.dbm, a.traces.max_hold.dbm)
    assert b.traces.live is not None and b.traces.average is not None
    assert b.trace_map()["ref1"] is not None
    assert "Opened" in b.message


def test_open_does_not_touch_device_and_stops_acquisition(tmp_path: Path) -> None:
    c1, f1 = make()
    configure(c1)
    assert f1.save(tmp_path / "a.opencoord")
    c2, f2 = make()
    c2.connect()
    deadline = time.monotonic() + 5
    while c2.state.connection != "connected":
        assert time.monotonic() < deadline
        c2.tick()
        time.sleep(0.005)
    c2.set_mode("live")
    c2.start()
    assert c2.state.running
    assert f2.open(tmp_path / "a.opencoord")
    assert not c2.state.running and c2.state.connection == "connected"
    assert c2.state.mode == "scan"
    c2.shutdown()


def test_open_errors_become_messages(tmp_path: Path) -> None:
    c, f = make()
    bad = tmp_path / "bad.opencoord"
    bad.write_bytes(b"nope")
    assert not f.open(bad)
    assert "Not an OpenCoord session" in c.state.message
    assert f.path is None


def test_save_without_path_is_refused_and_unwritable_reports(tmp_path: Path) -> None:
    c, f = make()
    assert not f.save()
    (tmp_path / "file").write_text("x")
    assert not f.save(tmp_path / "file" / "sub.opencoord")
    assert "Cannot save" in c.state.message


def test_save_keeps_created_timestamp(tmp_path: Path) -> None:
    _, f = make()
    assert f.save(tmp_path / "a.opencoord")
    first = f.build_session().created
    assert f.save()
    assert f.build_session().created == first


def test_exports(tmp_path: Path) -> None:
    c, f = make()
    feed(c)
    assert f.export("generic", "max", tmp_path / "m")
    text = (tmp_path / "m.csv").read_text()
    assert text.startswith("frequency_mhz,level_dbm\n590.000000,-100.0\n")
    assert f.export("wwb", "max", tmp_path / "w.csv")
    assert (tmp_path / "w.csv").read_text().startswith("590.000, -100.0\n")
    assert f.export("wsm", "live", tmp_path / "s.csv")
    assert f.export("carriers", "max", tmp_path / "c.csv")
    rows = (tmp_path / "c.csv").read_text().splitlines()
    assert rows[0] == "frequency_mhz,level_dbm,channel"
    assert rows[1].startswith("600.000000,-40.0,")


def test_export_png_and_missing_data(tmp_path: Path) -> None:
    c, f = make()
    assert not f.export("generic", "max", tmp_path / "x.csv")
    assert "no data" in c.state.message
    assert not f.export("png", "max", tmp_path / "x.png")
    img = np.zeros((4, 6, 4), dtype=np.uint8)
    assert f.export("png", "max", tmp_path / "x.png", img)
    assert (tmp_path / "x.png").read_bytes().startswith(b"\x89PNG")


def test_import_reference(tmp_path: Path) -> None:
    c, f = make()
    p = tmp_path / "wwb.csv"
    p.write_text("470.000, -109.0\n470.025, -108.0\n")
    assert f.import_reference(p)
    ref = c.state.references["ref1"]
    assert ref.freqs_hz.tolist() == [470 * MHZ, 470_025_000]
    assert ref.label == "Ref 1: wwb"
    bad = tmp_path / "bad.csv"
    bad.write_text("hello\n")
    assert not f.import_reference(bad)
    assert "Cannot import" in c.state.message


def test_with_suffix() -> None:
    assert with_suffix(Path("a"), ".csv") == Path("a.csv")
    assert with_suffix(Path("a.txt"), ".csv") == Path("a.txt")


def test_trace_choices() -> None:
    c, f = make()
    assert f.trace_choices() == []
    feed(c)
    assert [k for k, _ in f.trace_choices()] == ["live", "avg", "min", "max"]


def test_title() -> None:
    _, f = make()
    assert f.title.startswith("untitled")
