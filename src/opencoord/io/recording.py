"""Recordings: the ``.ocrec`` file (a zip of ``meta.json`` and chunked ``chunk_NNNNNN.npz``).

Pure codec: :func:`encode_chunk` / :func:`decode_chunk` (up to :data:`CHUNK_SWEEPS` sweeps a chunk)
and :func:`meta_to_json` / :func:`meta_from_json`. File I/O: :class:`RecordingWriter` and
:class:`RecordingReader`.

A chunk is a compressed ``.npz`` with per-sweep ``t`` (float64 timestamps), ``start_hz`` /
``step_hz`` / ``points`` (int64) and ``irregular`` (uint8), plus the concatenated ``dbm`` (float32)
of all sweeps. Sweeps may differ in axis. A sweep whose axis is not exactly
``start + i * step`` (a stitched scan can be slightly non-uniform) also stores its frequencies in
the concatenated ``freqs_irregular`` (float64), so every axis round-trips exactly.

Crash safety: while recording, chunks and ``meta.json`` are written (atomically, one file at a time)
into a directory ``<name>.ocrec.parts/``; closing zips them into ``<name>.ocrec`` (temporary file +
atomic replace) and deletes the directory. After a crash the flushed chunks are still there (at
most the last 256 sweeps / 5 s are lost): :meth:`RecordingReader.open` reads such a directory
directly (or via the final name when only the directory exists) and :func:`finalize_parts` zips
it. Chunk files not yet listed in ``meta.json`` (crash between the two writes) are picked up too.
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import zipfile
import zlib
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from opencoord import __version__
from opencoord.core.types import Sweep
from opencoord.io.atomic import write_atomic

SCHEMA_VERSION: Final = 1
RECORDING_SUFFIX: Final = ".ocrec"
PARTS_SUFFIX: Final = ".parts"
META_NAME: Final = "meta.json"
#: Sweeps per chunk, and the longest time sweeps stay in memory before a chunk is written.
CHUNK_SWEEPS: Final = 256
FLUSH_INTERVAL_S: Final = 5.0
#: Read limits (zip bomb guard): one chunk, and all chunks of a recording, uncompressed.
MAX_CHUNK_BYTES: Final = 256 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 8 * 1024 * 1024 * 1024
#: Most points a single sweep may have when reading.
MAX_POINTS: Final = 1 << 20
_READ_ERRORS: Final = (
    zlib.error,
    NotImplementedError,
    RuntimeError,
    KeyError,
    EOFError,
    OSError,
    ValueError,
    zipfile.BadZipFile,
)


class RecordingError(ValueError):
    """The file is not a readable recording, or cannot be written (message is user-facing)."""


@dataclass(frozen=True)
class RecordingInfo:
    """What the recording knows about the device it came from."""

    model_name: str
    model_code: int
    expansion_code: int | None
    firmware: str
    min_hz: int
    max_hz: int
    amp_top_dbm: float = -30.0
    amp_bottom_dbm: float = -120.0


@dataclass(frozen=True)
class ChunkInfo:
    name: str
    sweeps: int


@dataclass
class RecordingMeta:
    created: str = ""
    opencoord_version: str = ""
    sweep_count: int = 0
    chunks: list[ChunkInfo] = field(default_factory=list)
    device: RecordingInfo | None = None
    schema_version: int = SCHEMA_VERSION


def chunk_name(index: int) -> str:
    return f"chunk_{index:06d}.npz"


# --- chunk codec ----------------------------------------------------------------------------------


def encode_chunk(sweeps: Sequence[Sweep]) -> bytes:
    """Compressed ``.npz`` bytes for ``sweeps`` (at least one; axes may differ)."""
    if not sweeps:
        raise ValueError("a chunk needs at least one sweep")
    n = len(sweeps)
    start = np.empty(n, dtype=np.int64)
    step = np.empty(n, dtype=np.int64)
    points = np.empty(n, dtype=np.int64)
    irregular = np.zeros(n, dtype=np.uint8)
    extra: list[npt.NDArray[np.float64]] = []
    for i, s in enumerate(sweeps):
        f = np.asarray(s.freqs_hz, dtype=np.float64)
        count = len(f)
        start[i] = round(float(f[0]))
        step[i] = round(float(f[-1] - f[0]) / (count - 1)) if count > 1 else 0
        points[i] = count
        if not np.array_equal(_axis(int(start[i]), int(step[i]), count), f):
            irregular[i] = 1
            extra.append(f)
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        t=np.array([s.timestamp for s in sweeps], dtype=np.float64),
        start_hz=start,
        step_hz=step,
        points=points,
        irregular=irregular,
        dbm=np.concatenate([np.asarray(s.dbm, dtype=np.float32) for s in sweeps]),
        freqs_irregular=np.concatenate(extra) if extra else np.zeros(0, dtype=np.float64),
    )
    return buf.getvalue()


def _axis(start: int, step: int, points: int) -> npt.NDArray[np.float64]:
    return start + np.arange(points, dtype=np.float64) * step


def decode_chunk(data: bytes) -> list[Sweep]:
    """The sweeps in a chunk; :class:`RecordingError` for unreadable or inconsistent data."""
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            if sum(i.file_size for i in npz.zip.infolist()) > MAX_CHUNK_BYTES:
                raise RecordingError("A chunk of the recording is too large")
            t = np.asarray(npz["t"], dtype=np.float64)
            start = np.asarray(npz["start_hz"], dtype=np.int64)
            step = np.asarray(npz["step_hz"], dtype=np.int64)
            points = np.asarray(npz["points"], dtype=np.int64)
            irregular = np.asarray(npz["irregular"], dtype=np.uint8)
            dbm = np.asarray(npz["dbm"], dtype=np.float32)
            extra = np.asarray(npz["freqs_irregular"], dtype=np.float64)
    except RecordingError:
        raise
    except _READ_ERRORS as exc:
        raise RecordingError(f"A chunk of the recording is unreadable: {exc}") from exc
    n = len(t)
    shapes_ok = all(a.shape == (n,) for a in (start, step, points, irregular))
    if (
        not shapes_ok
        or n == 0
        or dbm.ndim != 1
        or extra.ndim != 1
        or points.min() < 1
        or points.max() > MAX_POINTS
        or int(points.sum()) != len(dbm)
        or int(points[irregular != 0].sum()) != len(extra)
    ):
        raise RecordingError("A chunk of the recording has inconsistent data")
    out: list[Sweep] = []
    offset = extra_offset = 0
    for i in range(n):
        count = int(points[i])
        if irregular[i]:
            freqs = extra[extra_offset : extra_offset + count].copy()
            extra_offset += count
        else:
            freqs = _axis(int(start[i]), int(step[i]), count)
        out.append(Sweep(freqs, dbm[offset : offset + count].copy(), float(t[i])))
        offset += count
    return out


# --- meta.json ------------------------------------------------------------------------------------


def meta_to_json(meta: RecordingMeta) -> str:
    d = meta.device
    doc: dict[str, Any] = {
        "schema_version": meta.schema_version,
        "opencoord_version": meta.opencoord_version,
        "created": meta.created,
        "sweep_count": meta.sweep_count,
        "chunks": [{"name": c.name, "sweeps": c.sweeps} for c in meta.chunks],
        "device": None
        if d is None
        else {
            "model_name": d.model_name,
            "model_code": d.model_code,
            "expansion_code": d.expansion_code,
            "firmware": d.firmware,
            "min_hz": d.min_hz,
            "max_hz": d.max_hz,
            "amp_top_dbm": d.amp_top_dbm,
            "amp_bottom_dbm": d.amp_bottom_dbm,
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _typed(doc: dict[str, Any], key: str, kind: type | tuple[type, ...], default: Any) -> Any:
    value = doc.get(key, default)
    if value is None:
        return default
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if (isinstance(value, bool) and bool not in kinds) or not isinstance(value, kind):
        raise RecordingError(f"Invalid recording: '{key}' has the wrong type")
    return value


def meta_from_json(text: str) -> RecordingMeta:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise RecordingError(f"The recording's meta.json is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RecordingError("Invalid recording: meta.json must be a JSON object")
    version = doc.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise RecordingError("Invalid recording: 'schema_version' is missing")
    if version > SCHEMA_VERSION:
        raise RecordingError(
            f"This recording uses format version {version}, which is newer than this OpenCoord "
            f"understands ({SCHEMA_VERSION}); update OpenCoord to open it"
        )
    if version < 1:
        raise RecordingError(f"Unsupported recording format version {version}")
    chunks = []
    for c in _typed(doc, "chunks", list, []):
        if not isinstance(c, dict):
            raise RecordingError("Invalid recording: 'chunks' must be a list of objects")
        chunks.append(ChunkInfo(_typed(c, "name", str, ""), _typed(c, "sweeps", int, 0)))
    raw = _typed(doc, "device", dict, None)
    device = None
    if raw is not None:
        device = RecordingInfo(
            model_name=_typed(raw, "model_name", str, ""),
            model_code=_typed(raw, "model_code", int, 0),
            expansion_code=_typed(raw, "expansion_code", int, None),
            firmware=_typed(raw, "firmware", str, ""),
            min_hz=_typed(raw, "min_hz", int, 0),
            max_hz=_typed(raw, "max_hz", int, 0),
            amp_top_dbm=float(_typed(raw, "amp_top_dbm", (int, float), -30.0)),
            amp_bottom_dbm=float(_typed(raw, "amp_bottom_dbm", (int, float), -120.0)),
        )
    return RecordingMeta(
        created=_typed(doc, "created", str, ""),
        opencoord_version=_typed(doc, "opencoord_version", str, ""),
        sweep_count=_typed(doc, "sweep_count", int, 0),
        chunks=chunks,
        device=device,
        schema_version=version,
    )


# --- writer ---------------------------------------------------------------------------------------


def parts_dir_for(path: Path) -> Path:
    return path.with_name(path.name + PARTS_SUFFIX)


def with_suffix(path: Path) -> Path:
    """``path`` with ``.ocrec`` appended unless it already has an extension."""
    return path if path.suffix else path.with_name(path.name + RECORDING_SUFFIX)


class PartsExistError(RecordingError):
    """A non-empty ``<name>.ocrec.parts`` directory (an unfinished recording) is in the way."""

    def __init__(self, parts: Path) -> None:
        super().__init__(
            f"{parts.name} is an unfinished recording (the app stopped before it was saved)"
        )
        self.parts = parts


_FINALIZE: Final = object()
#: Chunks handed to the writer thread at once; beyond that the UI side keeps buffering.
MAX_QUEUED_CHUNKS: Final = 8


class RecordingWriter:
    """Appends sweeps; a writer thread encodes and writes a chunk per 256 sweeps or 5 seconds.

    Nothing here blocks the caller: :meth:`append` / :meth:`poll` only buffer and hand finished
    chunks to a bounded queue (when it is full the chunks stay buffered here and are retried on the
    next call). Call :meth:`poll` regularly (every frame) so the time-based cut also happens while
    no sweeps arrive. :meth:`close` is non-blocking too: when the writer thread has written every
    chunk and zipped the file, :attr:`done` becomes true; check :attr:`error` for a disk failure
    (the flushed chunks stay in the parts directory and are recoverable). The 5 s clock starts when
    a sweep enters an empty buffer.
    """

    def __init__(
        self,
        path: Path,
        info: RecordingInfo | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self._clock = clock
        self._parts = parts_dir_for(path)
        if self._parts.exists() and any(self._parts.iterdir()):
            raise PartsExistError(self._parts)
        self._parts.mkdir(parents=True, exist_ok=True)
        self._meta = RecordingMeta(
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            opencoord_version=__version__,
            device=info,
        )
        self._write_meta()
        self._buffer: list[Sweep] = []
        self._buffer_since = 0.0
        self._pending: deque[list[Sweep]] = deque()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=MAX_QUEUED_CHUNKS)
        self._appended = 0
        self._closing = False
        self._sentinel_sent = False
        self._done = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="recording-writer", daemon=True)
        self._thread.start()

    @property
    def sweep_count(self) -> int:
        """Sweeps appended so far (written or not)."""
        return self._appended

    @property
    def closed(self) -> bool:
        """:meth:`close` was called (the file may still be being finished)."""
        return self._closing

    @property
    def done(self) -> bool:
        """The writer thread has finished (the file is saved, or :attr:`error` is set)."""
        return self._done.is_set()

    @property
    def error(self) -> Exception | None:
        return self._error

    def append(self, sweep: Sweep) -> None:
        if self._closing:
            raise RecordingError("The recording is already closed")
        if not self._buffer:
            self._buffer_since = self._clock()
        self._buffer.append(sweep)
        self._appended += 1
        self.poll()

    def poll(self) -> None:
        """Cut a chunk if one is full or its first sweep is 5 s old; hand chunks to the thread."""
        if self._buffer and (
            len(self._buffer) >= CHUNK_SWEEPS
            or self._clock() - self._buffer_since >= FLUSH_INTERVAL_S
        ):
            self._cut()
        self._pump()

    def flush(self) -> None:
        """Hand everything buffered to the writer thread now."""
        self._cut()
        self._pump()

    def _cut(self) -> None:
        if self._buffer:
            self._pending.append(self._buffer)
            self._buffer = []

    def _pump(self) -> None:
        while self._pending:
            try:
                self._queue.put_nowait(self._pending[0])
            except queue.Full:
                return
            self._pending.popleft()
        if self._closing and not self._sentinel_sent:
            try:
                self._queue.put_nowait(_FINALIZE)
            except queue.Full:
                return
            self._sentinel_sent = True

    def close(self) -> None:
        """Start finishing the file (non-blocking); poll :attr:`done`, then check :attr:`error`."""
        if not self._closing:
            self._closing = True
            self._cut()
            self._pump()

    def wait(self, timeout_s: float) -> bool:
        """Block until :attr:`done` (up to ``timeout_s``); only for shutdown and tests."""
        deadline = time.monotonic() + timeout_s
        while not self._done.is_set() and time.monotonic() < deadline:
            self.poll()
            self._done.wait(0.01)
        return self._done.is_set()

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        """Block until every handed-over chunk is on disk (tests)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._pump()
            if not self._pending and self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False

    # --- writer thread ------------------------------------------------------------------------

    def _write_meta(self) -> None:
        write_atomic(self._parts / META_NAME, meta_to_json(self._meta).encode("utf-8"))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _FINALIZE:
                    _zip_parts(self._parts, self.path)
                    return
                assert isinstance(item, list)
                name = chunk_name(len(self._meta.chunks))
                write_atomic(self._parts / name, encode_chunk(item))
                self._meta.chunks.append(ChunkInfo(name, len(item)))
                self._meta.sweep_count += len(item)
                self._write_meta()
            except Exception as exc:
                self._error = exc
                return
            finally:
                self._queue.task_done()
                if item is _FINALIZE or self._error is not None:
                    self._done.set()


class FinalizeJob:
    """Runs :func:`finalize_parts` on a background thread (recovering a crashed recording)."""

    def __init__(self, parts: Path) -> None:
        self.parts = parts
        self.path: Path | None = None
        self.error: Exception | None = None
        self.sweeps = 0
        self._done = threading.Event()
        threading.Thread(target=self._run, name="recording-recover", daemon=True).start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _run(self) -> None:
        try:
            self.sweeps = RecordingReader.open(self.parts).total_sweeps
            self.path = finalize_parts(self.parts)
        except Exception as exc:
            self.error = exc
        finally:
            self._done.set()


def _zip_parts(parts: Path, target: Path) -> None:
    """Zip a parts directory (``meta.json`` first, chunks stored) to ``target`` atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            with zipfile.ZipFile(f, "w") as z:
                z.write(parts / META_NAME, META_NAME, compress_type=zipfile.ZIP_DEFLATED)
                for chunk in sorted(parts.glob("chunk_*.npz")):
                    # Already compressed by np.savez_compressed: store them.
                    z.write(chunk, chunk.name, compress_type=zipfile.ZIP_STORED)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    shutil.rmtree(parts, ignore_errors=True)


def recovered_target(parts: Path) -> Path:
    """Where :func:`finalize_parts` writes: ``<name>.ocrec``, or ``<name>-recovered[-N].ocrec`` if
    that file exists (never replace an older, possibly good recording)."""
    target = parts.with_name(parts.name.removesuffix(PARTS_SUFFIX))
    if not target.exists():
        return target
    stem = target.name.removesuffix(RECORDING_SUFFIX)
    candidate = target.with_name(f"{stem}-recovered{RECORDING_SUFFIX}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = target.with_name(f"{stem}-recovered-{n}{RECORDING_SUFFIX}")
    return candidate


def finalize_parts(parts: Path) -> Path:
    """Zip an unfinalised ``<name>.ocrec.parts`` directory into ``<name>.ocrec`` (see
    :func:`recovered_target`); returns the file written."""
    reader = RecordingReader.open(parts)  # validates meta.json and the chunk files
    target = recovered_target(parts)
    if reader.orphans:  # chunks missing from meta.json: list them before zipping
        write_atomic(parts / META_NAME, meta_to_json(reader.meta).encode("utf-8"))
    _zip_parts(parts, target)
    return target


# --- reader ---------------------------------------------------------------------------------------


class RecordingReader:
    """Read access to a ``.ocrec`` file or an unfinalised ``.ocrec.parts`` directory."""

    def __init__(self, path: Path, meta: RecordingMeta, names: list[str], orphans: bool) -> None:
        self.path = path
        self.meta = meta
        #: Chunk member names in playback order.
        self.names = names
        #: Chunk files exist that ``meta.json`` did not list (unfinalised recording).
        self.orphans = orphans
        self._is_dir = path.is_dir()

    @property
    def total_sweeps(self) -> int:
        return sum(c.sweeps for c in self.meta.chunks)

    @classmethod
    def open(cls, path: Path) -> RecordingReader:
        if not path.exists():
            parts = parts_dir_for(path)
            if parts.is_dir():
                path = parts  # only the unfinalised directory exists
            else:
                raise RecordingError(f"File not found: {path}")
        return cls._open_dir(path) if path.is_dir() else cls._open_zip(path)

    @classmethod
    def _open_zip(cls, path: Path) -> RecordingReader:
        try:
            with zipfile.ZipFile(path) as z:
                names = sorted(n for n in z.namelist() if _is_chunk(n))
                if META_NAME not in z.namelist():
                    raise RecordingError(f"Not an OpenCoord recording: {META_NAME} is missing")
                if sum(i.file_size for i in z.infolist()) > MAX_TOTAL_BYTES:
                    raise RecordingError(f"{path.name} is too large to be a recording")
                meta = meta_from_json(z.read(META_NAME).decode("utf-8"))
        except RecordingError:
            raise
        except OSError as exc:
            raise RecordingError(f"Cannot read {path.name}: {exc.strerror or exc}") from exc
        except (*_READ_ERRORS, UnicodeDecodeError) as exc:
            raise RecordingError(
                f"Not a readable OpenCoord recording: {path.name} ({exc})"
            ) from exc
        return cls(path, meta, names, False)

    @classmethod
    def _open_dir(cls, path: Path) -> RecordingReader:
        meta_path = path / META_NAME
        try:
            meta = meta_from_json(meta_path.read_text("utf-8"))
        except FileNotFoundError as exc:
            raise RecordingError(
                f"Not an unfinished recording: {META_NAME} is missing in {path.name}"
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise RecordingError(f"Cannot read {path.name}: {exc}") from exc
        names = sorted(p.name for p in path.glob("chunk_*.npz") if _is_chunk(p.name))
        listed = {c.name for c in meta.chunks}
        orphans = [n for n in names if n not in listed]
        reader = cls(path, meta, names, bool(orphans))
        for name in orphans:  # a chunk written just before the crash: count it
            meta.chunks.append(ChunkInfo(name, len(reader.read_chunk(name))))
        meta.sweep_count = sum(c.sweeps for c in meta.chunks)
        return reader

    def read_chunk(self, name: str) -> list[Sweep]:
        try:
            if self._is_dir:
                data = (self.path / name).read_bytes()
            else:
                with zipfile.ZipFile(self.path) as z:
                    if z.getinfo(name).file_size > MAX_CHUNK_BYTES:
                        raise RecordingError(f"{name} in {self.path.name} is too large")
                    data = z.read(name)
        except RecordingError:
            raise
        except (*_READ_ERRORS,) as exc:
            raise RecordingError(f"Cannot read {name} in {self.path.name}: {exc}") from exc
        try:
            return decode_chunk(data)
        except RecordingError as exc:
            raise RecordingError(f"{name} in {self.path.name}: {exc}") from exc

    def sweeps(self) -> Iterator[Sweep]:
        """All sweeps in order, one chunk in memory at a time."""
        for name in self.names:
            yield from self.read_chunk(name)


def _is_chunk(name: str) -> bool:
    return name.startswith("chunk_") and name.endswith(".npz")


__all__ = [
    "CHUNK_SWEEPS",
    "FLUSH_INTERVAL_S",
    "RECORDING_SUFFIX",
    "ChunkInfo",
    "FinalizeJob",
    "PartsExistError",
    "RecordingError",
    "RecordingInfo",
    "RecordingMeta",
    "RecordingReader",
    "RecordingWriter",
    "decode_chunk",
    "encode_chunk",
    "finalize_parts",
    "meta_from_json",
    "meta_to_json",
    "parts_dir_for",
    "with_suffix",
]
