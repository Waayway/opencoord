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
        created="2026-10-09T10:00:00+00:00",
        modified="2026-10-09T11:00:00+00:00",
        opencoord_version="0.1.0",
    )


def test_json_round_trip() -> None:
    s = full_session()
    back = from_json(to_json(s))
    assert back == s
    assert json.loads(to_json(s))["schema_version"] == 1
    assert json.loads(to_json(s))["plan"] is None


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
        from_json('{"schema_version": 2}')


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
    ),
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
