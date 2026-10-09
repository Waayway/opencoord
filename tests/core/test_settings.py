"""AppSettings load/save (settings.toml)."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencoord.core import settings as settings_mod
from opencoord.core.settings import AppSettings, default_path, load, save


def test_defaults() -> None:
    s = AppSettings()
    assert s.last_port is None
    assert s.auto_connect is False
    assert s.preset == "Full UHF 470-960"
    assert s.resolution == "normal"
    assert (s.start_hz, s.stop_hz) == (470_000_000, 960_000_000)
    assert (s.window_width, s.window_height) == (1280, 800)
    assert s.waterfall_depth == 300
    assert s.mode == "live"


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.toml") == AppSettings()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "settings.toml"
    s = AppSettings(
        last_port="/dev/ttyUSB0",
        auto_connect=True,
        preset=None,
        resolution="fine",
        start_hz=600_000_000,
        stop_hz=700_000_000,
        window_width=1600,
        window_height=900,
        waterfall_depth=120,
        mode="scan",
    )
    save(s, path)
    assert path.read_text(encoding="utf-8").startswith("# OpenCoord settings")
    assert load(path) == s


def test_none_values_are_omitted(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    save(AppSettings(), path)
    text = path.read_text(encoding="utf-8")
    assert "last_port" not in text
    assert load(path) == AppSettings()


def test_bad_values_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        'last_port = 3\nauto_connect = "yes"\nresolution = "ultra"\nstart_hz = 800000000\n'
        "stop_hz = 700000000\nwaterfall_depth = 0\nwindow_width = 10\nmode = 'x'\n"
        'preset = "694-790"\nunknown = 1\n',
        encoding="utf-8",
    )
    s = load(path)
    d = AppSettings()
    assert s.preset == "694-790"
    assert (s.last_port, s.auto_connect, s.resolution, s.mode) == (
        d.last_port,
        d.auto_connect,
        d.resolution,
        d.mode,
    )
    assert (s.start_hz, s.stop_hz) == (d.start_hz, d.stop_hz)
    assert s.waterfall_depth == d.waterfall_depth
    assert s.window_width == d.window_width


def test_corrupt_file_gives_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("this is = = not toml", encoding="utf-8")
    assert load(path) == AppSettings()
    assert "settings" in caplog.text


def test_default_path_is_in_the_user_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_mod, "user_config_dir", lambda app: f"/cfg/{app}")
    assert default_path() == Path("/cfg/opencoord/settings.toml")


def test_invalid_utf8_gives_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.toml"
    path.write_bytes(b"\xff\xfe preset = 1")
    assert load(path) == AppSettings()
    assert "settings" in caplog.text


def test_waterfall_depth_limits(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("waterfall_depth = 1001\n", encoding="utf-8")
    assert load(path).waterfall_depth == 300
    path.write_text("waterfall_depth = 1000\n", encoding="utf-8")
    assert load(path).waterfall_depth == 1000


def test_threshold_roundtrip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    assert load(path).threshold_dbm is None
    save(AppSettings(threshold_dbm=-72.5), path)
    assert load(path).threshold_dbm == -72.5
    save(AppSettings(threshold_dbm=None), path)
    assert "threshold_dbm" not in path.read_text(encoding="utf-8")
    path.write_text("threshold_dbm = -70\n", encoding="utf-8")
    assert load(path).threshold_dbm == -70.0
    path.write_text('threshold_dbm = "high"\n', encoding="utf-8")
    assert load(path).threshold_dbm is None
    path.write_text("threshold_dbm = nan\n", encoding="utf-8")
    assert load(path).threshold_dbm is None


def test_amp_offsets_roundtrip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    assert load(path).amp_offsets == {}
    save(AppSettings(amp_offsets={"model_10": 2.5, "model_3": -1.0}), path)
    assert load(path).amp_offsets == {"model_10": 2.5, "model_3": -1.0}
    save(AppSettings(), path)
    assert "amp_offsets" not in path.read_text(encoding="utf-8")
    path.write_text(
        '[amp_offsets]\nmodel_10 = 3\nbad = "x"\nhuge = 900.0\nnan = nan\n', encoding="utf-8"
    )
    assert load(path).amp_offsets == {"model_10": 3.0}
    path.write_text("amp_offsets = 5\n", encoding="utf-8")
    assert load(path).amp_offsets == {}
