import contextlib
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.core import session as ses
from opencoord.core.markers import Marker
from opencoord.core.session import (
    DeviceInfo,
    Session,
    SessionError,
    SessionSettings,
    from_json,
    to_json,
)
from opencoord.core.types import ExclusionZone, Trace


def trace(label: str, n: int = 5, seed: int = 0) -> Trace:
    rng = np.random.default_rng(seed)
    return Trace(
        470e6 + 25e3 * np.arange(n, dtype=np.float64),
        rng.uniform(-110, -30, n).astype(np.float32),
        label,
    )


def full_session() -> Session:
    return Session(
        settings=SessionSettings(
            start_hz=470_000_000,
            stop_hz=960_000_000,
            mode="scan",
            preset="UHF TV",
            resolution="fine",
            threshold_dbm=-80.5,
            overlay_enabled=True,
            channel_plan="eu_dvbt",
        ),
        traces={"max": trace("Max", seed=1), "live": trace("Live", seed=2), "ref1": trace("R", 3)},
        markers=[Marker(1, 600_000_000, "max"), Marker(2, 601_000_000, "ref1")],
        exclusion_zones=[ExclusionZone(1, 500_000_000, 510_000_000)],
        device=DeviceInfo("WSUB1G+", 18, "01.36"),
        plan={"assignments": [{"label": "Mic #1", "freq_hz": 600_125_000}]},
        coordination={"devices": [{"profile": "Mic", "quantity": 2}]},
        created="2026-10-09T10:00:00+00:00",
        modified="2026-10-09T11:00:00+00:00",
        opencoord_version="0.1.0",
    )


def test_json_round_trip() -> None:
    s = full_session()
    back = from_json(to_json(s))
    assert back == s
    doc = json.loads(to_json(s))
    assert doc["schema_version"] == 2
    assert doc["plan"] == s.plan and doc["coordination"] == s.coordination


def test_save_load_round_trip(tmp_path: Path) -> None:
    s = full_session()
    p = tmp_path / "show.opencoord"
    ses.save(p, s)
    with zipfile.ZipFile(p) as z:
        assert sorted(z.namelist()) == ["session.json", "traces.npz"]
    back = ses.load(p)
    assert back == s
    assert set(back.traces) == set(s.traces)
    for k, t in s.traces.items():
        b = back.traces[k]
        assert b.label == t.label
        assert b.freqs_hz.dtype == np.float64 and b.dbm.dtype == np.float32
        assert np.array_equal(b.freqs_hz, t.freqs_hz) and np.array_equal(b.dbm, t.dbm)


def test_save_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    ses.save(tmp_path / "a.opencoord", full_session())
    assert [p.name for p in tmp_path.iterdir()] == ["a.opencoord"]


def test_missing_optional_fields_default() -> None:
    s = from_json('{"schema_version": 1}')
    assert s.markers == [] and s.exclusion_zones == [] and s.traces == {}
    assert s.device is None and s.plan is None
    assert s.settings.mode == "live" and s.settings.threshold_dbm is None


def test_future_version_is_clear_error() -> None:
    with pytest.raises(SessionError, match="newer"):
        from_json('{"schema_version": 3}')


def test_version_1_sessions_still_open() -> None:
    """Version 1 had a ``plan: null`` placeholder and no coordination setup."""
    s = from_json('{"schema_version": 1, "plan": null, "markers": [{"id": 1, "freq_hz": 5}]}')
    assert s.plan is None and s.coordination is None and len(s.markers) == 1


@pytest.mark.parametrize("key", ["plan", "coordination"])
def test_plan_and_coordination_must_be_objects(key: str) -> None:
    with pytest.raises(SessionError, match=key):
        from_json(f'{{"schema_version": 2, "{key}": [1]}}')


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", '{"schema_version": "x"}', "{}", '{"schema_version": 1, "markers": 3}'],
)
def test_bad_json(text: str) -> None:
    with pytest.raises(SessionError):
        from_json(text)


def test_load_errors(tmp_path: Path) -> None:
    p = tmp_path / "bad.opencoord"
    p.write_bytes(b"not a zip")
    with pytest.raises(SessionError):
        ses.load(p)
    with pytest.raises(SessionError):
        ses.load(tmp_path / "missing.opencoord")
    with zipfile.ZipFile(tmp_path / "empty.opencoord", "w") as z:
        z.writestr("other", "x")
    with pytest.raises(SessionError, match=r"session\.json"):
        ses.load(tmp_path / "empty.opencoord")


def test_missing_traces_npz_is_ok(tmp_path: Path) -> None:
    p = tmp_path / "a.opencoord"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("session.json", '{"schema_version": 1, "traces": {"max": {"label": "M"}}}')
    assert ses.load(p).traces == {}


def test_mismatched_trace_arrays_rejected() -> None:
    blob = ses.encode_traces({"max": trace("M")})
    arrays = ses.decode_traces(blob)
    arrays["max"] = (arrays["max"][0], arrays["max"][1][:2])
    with pytest.raises(SessionError):
        ses.build_traces({"max": "M"}, arrays)


def _saved(tmp_path: Path) -> bytes:
    ses.save(tmp_path / "ok.opencoord", full_session())
    return (tmp_path / "ok.opencoord").read_bytes()


def test_truncated_zip(tmp_path: Path) -> None:
    data = _saved(tmp_path)
    p = tmp_path / "t.opencoord"
    for cut in (10, len(data) // 2, len(data) - 5):
        p.write_bytes(data[:cut])
        with pytest.raises(SessionError):
            ses.load(p)


def test_every_flipped_byte_is_a_session_error_or_loads(tmp_path: Path) -> None:
    data = bytearray(_saved(tmp_path))
    p = tmp_path / "f.opencoord"
    for i in range(0, len(data), 7):
        broken = bytearray(data)
        broken[i] ^= 0xFF
        p.write_bytes(bytes(broken))
        with contextlib.suppress(SessionError):  # anything else escaping fails the test
            ses.load(p)


def test_corrupt_deflate_stream_in_npz(tmp_path: Path) -> None:
    blob = bytearray(ses.encode_traces({"max": trace("M", 200)}))
    blob[len(blob) // 2] ^= 0xFF
    p = tmp_path / "c.opencoord"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("session.json", '{"schema_version": 1}')
        z.writestr("traces.npz", bytes(blob))
    with contextlib.suppress(SessionError):
        ses.load(p)


def test_unsupported_compression_and_garbage_npz(tmp_path: Path) -> None:
    p = tmp_path / "bz.opencoord"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("session.json", '{"schema_version": 1}')
    assert ses.load(p).markers == []  # bzip2 is supported by zipfile
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("session.json", '{"schema_version": 1}')
        z.writestr("traces.npz", b"garbage")
    with pytest.raises(SessionError, match="unreadable"):
        ses.load(p)


def test_oversize_session_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ses, "MAX_UNCOMPRESSED_BYTES", 100)
    ses.save(tmp_path / "big.opencoord", full_session())
    with pytest.raises(SessionError, match="too large"):
        ses.load(tmp_path / "big.opencoord")


def test_non_finite_threshold_is_dropped() -> None:
    s = from_json('{"schema_version": 1, "settings": {"threshold_dbm": NaN}}')
    assert s.settings.threshold_dbm is None


@given(
    st.builds(
        SessionSettings,
        start_hz=st.integers(0, 6_000_000_000),
        stop_hz=st.integers(0, 6_000_000_000),
        mode=st.sampled_from(["live", "scan"]),
        preset=st.none() | st.text(max_size=20),
        resolution=st.sampled_from(["fast", "normal", "fine"]),
        threshold_dbm=st.none() | st.floats(-150, 20, allow_nan=False),
        overlay_enabled=st.booleans(),
        channel_plan=st.none() | st.text(max_size=20),
    ).filter(lambda s: s.start_hz < s.stop_hz),
    st.lists(
        st.builds(
            Marker,
            id=st.integers(1, 8),
            freq_hz=st.integers(0, 6_000_000_000),
            trace_key=st.text(min_size=1, max_size=8),
        ),
        max_size=8,
    ),
    st.lists(
        st.builds(
            ExclusionZone,
            id=st.integers(1, 16),
            start_hz=st.integers(0, 10**9),
            stop_hz=st.integers(0, 10**9),
        ),
        max_size=16,
    ),
    st.none()
    | st.builds(DeviceInfo, st.text(max_size=12), st.integers(0, 255), st.text(max_size=8)),
)
def test_json_round_trip_hypothesis(
    settings: SessionSettings,
    markers: list[Marker],
    zones: list[ExclusionZone],
    device: DeviceInfo | None,
) -> None:
    s = Session(
        settings=settings,
        markers=markers,
        exclusion_zones=zones,
        device=device,
        created="2026-01-01T00:00:00+00:00",
        modified="2026-01-02T00:00:00+00:00",
        opencoord_version="0.1.0",
    )
    assert from_json(to_json(s)) == s


def test_bare_npy_trace_data_is_a_session_error() -> None:
    import io as _io

    buf = _io.BytesIO()
    np.save(buf, np.zeros(3))
    with pytest.raises(ses.SessionError, match="trace data"):
        ses.decode_traces(buf.getvalue())


def test_session_with_bare_npy_traces_does_not_open(tmp_path: Path) -> None:
    import io as _io

    buf = _io.BytesIO()
    np.save(buf, np.zeros(3))
    path = tmp_path / "x.opencoord"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("session.json", json.dumps({"schema_version": 2, "traces": {"live": {}}}))
        z.writestr("traces.npz", buf.getvalue())
    with pytest.raises(ses.SessionError):
        ses.load(path)


@pytest.mark.parametrize(
    ("start", "stop"),
    [
        (-1, 100_000_000),
        (500_000_000, 500_000_000),
        (600_000_000, 500_000_000),
        (1, 100_000_000_001),
        (10**30, 10**31),
    ],
)
def test_out_of_range_settings_are_rejected(start: int, stop: int) -> None:
    doc = {"schema_version": 2, "settings": {"start_hz": start, "stop_hz": stop}}
    with pytest.raises(ses.SessionError, match="frequency range"):
        ses.from_json(json.dumps(doc))


def test_settings_range_limits_are_inclusive() -> None:
    doc = {"schema_version": 2, "settings": {"start_hz": 0, "stop_hz": 100_000_000_000}}
    s = ses.from_json(json.dumps(doc)).settings
    assert (s.start_hz, s.stop_hz) == (0, 100_000_000_000)


@pytest.mark.parametrize(
    "freqs", [[1e8, 1e8, 2e8], [3e8, 2e8, 1e8], [1e8, 3e8, 2e8]], ids=["flat", "down", "zigzag"]
)
def test_traces_must_have_strictly_increasing_frequencies(freqs: list[float]) -> None:
    arrays = {"live": (np.array(freqs), np.zeros(3, dtype=np.float32))}
    with pytest.raises(ses.SessionError, match="increasing"):
        ses.build_traces({"live": "Live"}, arrays)


def test_single_point_trace_is_accepted() -> None:
    arrays = {"live": (np.array([1e8]), np.zeros(1, dtype=np.float32))}
    assert len(ses.build_traces({"live": "Live"}, arrays)["live"].dbm) == 1
