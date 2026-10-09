"""``.ocrec`` format: chunk codec, writer (finalise + crash recovery), reader, corrupt files."""

from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from opencoord.core.types import Sweep
from opencoord.io import recording
from opencoord.io.recording import (
    RecordingError,
    RecordingInfo,
    RecordingReader,
    RecordingWriter,
)

INFO = RecordingInfo(
    model_name="RF Explorer WSUB1G+",
    model_code=10,
    expansion_code=None,
    firmware="03.39",
    min_hz=50_000,
    max_hz=960_000_000,
    amp_top_dbm=-30.0,
    amp_bottom_dbm=-120.0,
)


def sweep(
    t: float, start: int = 470_000_000, step: int = 25_000, points: int = 8, level: float = -90.0
) -> Sweep:
    freqs = start + np.arange(points, dtype=np.float64) * step
    dbm = (level + np.arange(points, dtype=np.float32)).astype(np.float32)
    return Sweep(freqs, dbm, t)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def same(a: Sweep, b: Sweep) -> bool:
    return (
        a.timestamp == b.timestamp
        and np.array_equal(a.freqs_hz, b.freqs_hz)
        and np.array_equal(a.dbm, b.dbm)
    )


def test_chunk_round_trip_with_mixed_axes() -> None:
    sweeps = [
        sweep(1.0),
        sweep(1.5, start=600_000_000, step=100_000, points=50),
        sweep(2.0, points=1),
        sweep(2.5, points=2),
    ]
    out = recording.decode_chunk(recording.encode_chunk(sweeps))
    assert len(out) == len(sweeps)
    assert all(same(a, b) for a, b in zip(sweeps, out, strict=True))
    assert out[0].freqs_hz.dtype == np.float64 and out[0].dbm.dtype == np.float32


def test_irregular_axis_is_kept_exactly() -> None:
    freqs = np.array([100e6, 100.0e6 + 25_000.4, 100e6 + 51_000.0, 100e6 + 80_000.0])
    s = Sweep(freqs, np.array([-1, -2, -3, -4], dtype=np.float32), 5.0)
    (out,) = recording.decode_chunk(recording.encode_chunk([s]))
    assert same(s, out)


def test_encode_empty_chunk_is_refused() -> None:
    with pytest.raises(ValueError):
        recording.encode_chunk([])


def test_decode_chunk_rejects_garbage_and_inconsistent_data() -> None:
    with pytest.raises(RecordingError):
        recording.decode_chunk(b"not an npz")
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        t=np.zeros(1),
        start_hz=np.zeros(1, np.int64),
        step_hz=np.ones(1, np.int64),
        points=np.array([5], np.int64),
        irregular=np.zeros(1, np.uint8),
        dbm=np.zeros(3, np.float32),  # 5 points announced, 3 present
        freqs_irregular=np.zeros(0),
    )
    with pytest.raises(RecordingError, match="inconsistent"):
        recording.decode_chunk(buf.getvalue())


def test_meta_json_round_trip_and_version_check() -> None:
    meta = recording.RecordingMeta(
        created="2026-10-09T10:00:00+00:00",
        opencoord_version="0.1.0",
        sweep_count=3,
        chunks=[recording.ChunkInfo("chunk_000000.npz", 3)],
        device=INFO,
    )
    assert recording.meta_from_json(recording.meta_to_json(meta)) == meta
    doc = json.loads(recording.meta_to_json(meta))
    assert doc["schema_version"] == 1
    doc["schema_version"] = 2
    with pytest.raises(RecordingError, match="newer"):
        recording.meta_from_json(json.dumps(doc))
    with pytest.raises(RecordingError):
        recording.meta_from_json("{broken")
    with pytest.raises(RecordingError):
        recording.meta_from_json("[]")


def write(path: Path, sweeps: list[Sweep]) -> Path:
    w = RecordingWriter(path, INFO, clock=FakeClock())
    for s in sweeps:
        w.append(s)
    w.close()
    assert w.wait(10) and w.error is None
    return w.path


def test_writer_round_trip_across_chunks(tmp_path: Path) -> None:
    sweeps = [sweep(i * 0.1, level=-100.0 + i % 7) for i in range(600)]
    sweeps[300] = sweep(30.0, start=600_000_000, step=50_000, points=20)  # mixed axis
    path = write(tmp_path / "a.ocrec", sweeps)
    assert path == tmp_path / "a.ocrec"
    assert not (tmp_path / "a.ocrec.parts").exists()
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == [
            "chunk_000000.npz",
            "chunk_000001.npz",
            "chunk_000002.npz",
            "meta.json",
        ]
    r = RecordingReader.open(path)
    assert r.meta.sweep_count == 600 and r.total_sweeps == 600
    assert [c.sweeps for c in r.meta.chunks] == [256, 256, 88]
    assert r.meta.device == INFO and r.meta.schema_version == 1
    out = list(r.sweeps())
    assert len(out) == 600 and all(same(a, b) for a, b in zip(sweeps, out, strict=True))


def test_empty_recording_is_valid(tmp_path: Path) -> None:
    path = write(tmp_path / "e.ocrec", [])
    r = RecordingReader.open(path)
    assert r.total_sweeps == 0 and list(r.sweeps()) == []


def test_writer_flushes_every_five_seconds(tmp_path: Path) -> None:
    clock = FakeClock()
    w = RecordingWriter(tmp_path / "t.ocrec", INFO, clock=clock)
    parts = tmp_path / "t.ocrec.parts"
    w.append(sweep(1.0))
    assert w.wait_idle() and not list(parts.glob("chunk_*"))
    clock.now += 4.9
    w.poll()
    assert w.wait_idle() and not list(parts.glob("chunk_*"))
    clock.now += 0.2
    w.poll()
    assert w.wait_idle()
    assert [p.name for p in parts.glob("chunk_*")] == ["chunk_000000.npz"]
    w.append(sweep(2.0))
    clock.now += 5.1
    w.append(sweep(3.0))  # append also checks the time
    assert w.wait_idle() and len(list(parts.glob("chunk_*"))) == 2
    w.close()
    assert w.wait(5)
    assert len(list(RecordingReader.open(tmp_path / "t.ocrec").sweeps())) == 3


def test_flushed_chunks_survive_a_crash(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "c.ocrec", INFO, clock=FakeClock())
    for i in range(300):  # one full chunk flushed, 44 sweeps still buffered
        w.append(sweep(i * 0.1))
    parts = tmp_path / "c.ocrec.parts"
    assert w.wait_idle()
    assert (parts / "chunk_000000.npz").exists() and (parts / "meta.json").exists()
    assert not (tmp_path / "c.ocrec").exists()
    # "Crash": the writer is abandoned. The parts directory opens like a recording.
    r = RecordingReader.open(parts)
    assert r.total_sweeps == 256
    assert len(list(r.sweeps())) == 256
    # ... and so does the final name, which resolves to the unfinalised directory.
    assert RecordingReader.open(tmp_path / "c.ocrec").total_sweeps == 256
    final = recording.finalize_parts(parts)
    assert final == tmp_path / "c.ocrec" and not parts.exists()
    assert len(list(RecordingReader.open(final).sweeps())) == 256


def test_orphan_chunk_without_meta_update_is_recovered(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "o.ocrec", INFO, clock=FakeClock())
    for i in range(256):
        w.append(sweep(i * 0.1))
    parts = tmp_path / "o.ocrec.parts"
    assert w.wait_idle()
    # Crash between writing chunk 1 and updating meta.json.
    (parts / "chunk_000001.npz").write_bytes(recording.encode_chunk([sweep(100.0), sweep(100.1)]))
    r = RecordingReader.open(parts)
    assert r.total_sweeps == 258
    assert len(list(r.sweeps())) == 258


def test_missing_meta_in_parts_is_an_error(tmp_path: Path) -> None:
    parts = tmp_path / "m.ocrec.parts"
    parts.mkdir()
    (parts / "chunk_000000.npz").write_bytes(recording.encode_chunk([sweep(1.0)]))
    with pytest.raises(RecordingError, match=r"meta\.json"):
        RecordingReader.open(parts)


def test_stale_parts_directory_is_not_overwritten(tmp_path: Path) -> None:
    clock = FakeClock()
    w = RecordingWriter(tmp_path / "s.ocrec", INFO, clock=clock)
    w.append(sweep(1.0))
    clock.now += 6
    w.poll()  # flushes the first chunk
    assert w.wait_idle()
    with pytest.raises(recording.PartsExistError, match="unfinished recording"):
        RecordingWriter(tmp_path / "s.ocrec", INFO, clock=FakeClock())


def test_corrupt_files_give_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(RecordingError, match="not found"):
        RecordingReader.open(tmp_path / "missing.ocrec")
    junk = tmp_path / "junk.ocrec"
    junk.write_bytes(b"this is not a zip")
    with pytest.raises(RecordingError, match=r"junk\.ocrec"):
        RecordingReader.open(junk)
    nometa = tmp_path / "nometa.ocrec"
    with zipfile.ZipFile(nometa, "w") as z:
        z.writestr("chunk_000000.npz", b"x")
    with pytest.raises(RecordingError, match=r"meta\.json"):
        RecordingReader.open(nometa)
    # Valid container, damaged chunk: the error surfaces when the chunk is read.
    good = write(tmp_path / "g.ocrec", [sweep(1.0)])
    bad = tmp_path / "bad.ocrec"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, b"corrupt" if n.startswith("chunk") else zin.read(n))
    r = RecordingReader.open(bad)
    with pytest.raises(RecordingError, match=r"chunk_000000\.npz"):
        list(r.sweeps())


def test_zip_bomb_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path / "b.ocrec", [sweep(i) for i in range(10)])
    monkeypatch.setattr(recording, "MAX_TOTAL_BYTES", 100)
    with pytest.raises(RecordingError, match="too large"):
        RecordingReader.open(path)
    monkeypatch.setattr(recording, "MAX_TOTAL_BYTES", 10**9)
    monkeypatch.setattr(recording, "MAX_CHUNK_BYTES", 100)
    with pytest.raises(RecordingError, match="too large"):
        list(RecordingReader.open(path).sweeps())


def test_close_is_idempotent_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "x.ocrec"
    w = RecordingWriter(target, INFO, clock=FakeClock())
    w.append(sweep(1.0))
    assert not target.exists()  # appears only when finished
    w.close()
    w.close()
    assert w.wait(5) and w.error is None and w.done
    assert len(list(RecordingReader.open(target).sweeps())) == 1
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    with pytest.raises(RecordingError):
        w.append(sweep(2.0))


def test_existing_recording_is_never_replaced(tmp_path: Path) -> None:
    target = tmp_path / "x.ocrec"
    target.write_bytes(b"old")
    with pytest.raises(RecordingError, match="already exists"):
        RecordingWriter(target, INFO, clock=FakeClock())
    assert target.read_bytes() == b"old" and not (tmp_path / "x.ocrec.parts").exists()


def test_debris_of_a_saved_recording_does_not_block_the_name(tmp_path: Path) -> None:
    path = write(tmp_path / "d.ocrec", [sweep(1.0)])
    parts = tmp_path / "d.ocrec.parts"
    parts.mkdir()
    (parts / "chunk_000000.npz").write_bytes(b"left over")  # rmtree was interrupted: no meta.json
    with pytest.raises(RecordingError, match="already exists"):
        RecordingWriter(path, INFO, clock=FakeClock())
    assert not parts.exists()
    assert not (tmp_path / "d.ocrec.parts").exists()


def test_finishing_removes_meta_before_the_rest(tmp_path: Path) -> None:
    write(tmp_path / "m.ocrec", [sweep(1.0)])
    assert not (tmp_path / "m.ocrec.parts").exists()


def test_the_caller_never_blocks_and_a_full_queue_keeps_buffering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = recording.write_atomic

    def slow(path: Path, data: bytes) -> None:
        if path.name.startswith("chunk"):
            gate.wait(10)
        real(path, data)

    monkeypatch.setattr(recording, "write_atomic", slow)
    w = RecordingWriter(tmp_path / "q.ocrec", INFO, clock=FakeClock())
    t0 = time.monotonic()
    total = recording.CHUNK_SWEEPS * (recording.MAX_QUEUED_CHUNKS + 4)
    for i in range(total):  # the writer thread is stuck: 12 chunks cannot all be queued
        w.append(sweep(float(i)))
    w.close()  # does not block either
    assert time.monotonic() - t0 < 2.0
    assert not w.done and w.sweep_count == total
    gate.set()
    assert w.wait(20) and w.error is None
    assert len(list(RecordingReader.open(tmp_path / "q.ocrec").sweeps())) == total


def test_disk_failure_in_the_writer_thread_is_reported(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "f.ocrec", INFO, clock=FakeClock())
    shutil.rmtree(tmp_path / "f.ocrec.parts")
    (tmp_path / "f.ocrec.parts").write_text("in the way")
    w.append(sweep(1.0))
    w.close()
    assert w.wait(5)
    assert isinstance(w.error, OSError)


def test_time_flush_clock_starts_with_the_first_sweep(tmp_path: Path) -> None:
    clock = FakeClock()
    w = RecordingWriter(tmp_path / "t2.ocrec", INFO, clock=clock)
    clock.now += 100  # idle before the first sweep must not count
    w.append(sweep(1.0))
    assert w.wait_idle() and not list((tmp_path / "t2.ocrec.parts").glob("chunk_*"))
    w.close()
    assert w.wait(5)


def test_recover_does_not_replace_an_existing_recording(tmp_path: Path) -> None:
    w = RecordingWriter(tmp_path / "r.ocrec", INFO, clock=FakeClock())
    for i in range(256):
        w.append(sweep(float(i)))
    assert w.wait_idle()
    good = write(tmp_path / "other.ocrec", [sweep(1.0)])
    shutil.copy(good, tmp_path / "r.ocrec")  # a good file took the name meanwhile
    parts = tmp_path / "r.ocrec.parts"
    job = recording.FinalizeJob(parts)
    deadline = time.monotonic() + 5
    while not job.done:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert job.error is None and job.sweeps == 256
    assert job.path == tmp_path / "r-recovered.ocrec" and not parts.exists()
    assert len(list(RecordingReader.open(tmp_path / "r.ocrec").sweeps())) == 1  # untouched
    assert len(list(RecordingReader.open(job.path).sweeps())) == 256
