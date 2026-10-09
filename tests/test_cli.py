"""The debug CLI: info/sweep via the simulator, record via a fake serial port."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from opencoord import __version__
from opencoord.cli import main
from opencoord.device import protocol

FIXTURES = Path(__file__).parent / "fixtures"
F1 = (FIXTURES / "wsub1gplus_config_and_sweeps.bin").read_bytes()
F1_COMPLETE = F1[: F1.rfind(b"\r\n$S") + 2]
C0 = protocol.request_config()


class FakeSerial:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.closed = False

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def write(self, data: bytes, /) -> int:
        if bytes(data) == C0:
            with self._lock:
                self._buf += F1_COMPLETE
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._buf[: min(size, 97)])
            del self._buf[: len(out)]
        if not out:
            time.sleep(0.002)
        return out

    def close(self) -> None:
        self.closed = True


class Port:
    device = "/dev/ttyFAKE0"
    vid = 0x10C4
    pid = 0xEA60
    description = "fake"


def test_info_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulator", "info"]) == 0
    out = capsys.readouterr().out
    assert "Model:" in out and "Firmware:" in out and "Configuration:" in out


def test_info_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulator", "info", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["capabilities"]["max_hz"] > data["capabilities"]["min_hz"]
    assert data["config"]["sweep_points"] > 0


def test_sweep_prints_mhz_dbm_rows(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulator", "sweep", "--start", "470", "--stop", "700"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "MHz,dBm"
    first, last = lines[1].split(","), lines[-1].split(",")
    assert float(first[0]) == pytest.approx(470.0, abs=0.01)
    assert float(last[0]) == pytest.approx(700.0, abs=0.5)
    float(first[1])


def test_sweep_count_prefixes_index(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--simulator", "sweep", "--start", "470", "--stop", "700", "--count", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "sweep,MHz,dBm"
    assert {line.split(",")[0] for line in lines[1:]} == {"0", "1"}


def test_sweep_max_hold_is_one_table(tmp_path: Path) -> None:
    out = tmp_path / "s.csv"
    args = ["--simulator", "sweep", "--start", "470", "--stop", "700", "--count", "3"]
    assert main([*args, "--max-hold", "--csv", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "MHz,dBm"
    assert all(line.count(",") == 1 for line in lines)


def test_sweep_rejects_bad_range() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--simulator", "sweep", "--start", "700", "--stop", "470"])
    assert exc.value.code == 2


def test_scan_writes_stitched_csv_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "scan.csv"
    args = ["--simulator", "scan", "--start", "470", "--stop", "500", "--resolution", "fast"]
    assert main([*args, "--csv", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "MHz,dBm"
    freqs = [float(line.split(",")[0]) for line in lines[1:]]
    assert freqs[0] == pytest.approx(470.0, abs=0.01)
    assert freqs[-1] == pytest.approx(500.0, abs=0.2)
    assert freqs == sorted(freqs)
    err = capsys.readouterr().err
    assert "fast" in err and "points" in err and "kHz bins" in err


def test_scan_rejects_bad_range_and_resolution() -> None:
    for bad in (["--start", "700", "--stop", "470"], ["--resolution", "ultra"]):
        with pytest.raises(SystemExit) as exc:
            main(["--simulator", "scan", "--start", "470", "--stop", "700", *bad])
        assert exc.value.code == 2


def test_no_command_is_usage_error() -> None:
    assert main([]) == 2


def test_record_rejects_simulator() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--simulator", "record", "--raw", "x.bin", "--seconds", "1"])
    assert exc.value.code == 2


def test_record_writes_raw_and_sidecar(tmp_path: Path) -> None:
    raw = tmp_path / "cap.bin"
    code = main(
        ["--port", "/dev/ttyFAKE0", "record", "--raw", str(raw), "--seconds", "0.2"],
        serial_factory=lambda port, baud: FakeSerial(),
        port_lister=lambda: [Port()],
    )
    assert code == 0
    data = raw.read_bytes()
    assert data == F1_COMPLETE
    side = json.loads(raw.with_suffix(".json").read_text())
    assert side["port"] == "/dev/ttyFAKE0"
    assert side["baud"] == 500_000
    assert side["firmware"] == "03.39" and side["model_code"] == 10
    assert side["settings"]["sweep_points"] > 0
    assert side["bytes"] == len(data)
    assert side["opencoord_version"] == __version__


def test_device_error_exits_1(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    def broken(port: str, baud: int) -> FakeSerial:
        raise FileNotFoundError(2, "no such device")

    code = main(["--port", "/dev/ttyNOPE", "info"], serial_factory=broken, port_lister=lambda: [])
    assert code == 1
    assert "not found" in capsys.readouterr().err
