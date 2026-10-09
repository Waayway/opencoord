"""Sessions: the ``.opencoord`` file (a zip with ``session.json`` and ``traces.npz``).

``to_json``/``from_json`` and ``encode_traces``/``decode_traces`` are pure; ``save``/``load`` do the
zip I/O. ``session.json`` carries ``schema_version`` (2; version 1 files, whose ``plan`` was an
always-null placeholder, still open); a newer version is refused with a clear error and missing
optional fields fall back to defaults. ``plan`` (a solved frequency plan) and ``coordination``
(the Coordination tab's setup) are JSON objects kept as dicts here; the UI layer decodes them
(``ui/coordination_model.py``). ``traces.npz`` holds, per trace name,
``<name>.freqs_hz`` (float64) and ``<name>.dbm`` (float32); the labels live in ``session.json``.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from opencoord.core.markers import Marker
from opencoord.core.types import ExclusionZone, Trace
from opencoord.io.atomic import write_atomic

SCHEMA_VERSION: Final = 2
SESSION_SUFFIX: Final = ".opencoord"
_JSON_NAME: Final = "session.json"
_NPZ_NAME: Final = "traces.npz"
#: Largest total uncompressed size accepted when reading a session (zip bomb guard).
MAX_UNCOMPRESSED_BYTES: Final = 256 * 1024 * 1024
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

Arrays = dict[str, tuple[npt.NDArray[np.float64], npt.NDArray[np.float32]]]


class SessionError(ValueError):
    """The file is not a readable session (message is user-facing)."""


@dataclass(frozen=True)
class SessionSettings:
    start_hz: int = 470_000_000
    stop_hz: int = 960_000_000
    mode: str = "live"
    preset: str | None = None
    resolution: str = "normal"
    threshold_dbm: float | None = None
    overlay_enabled: bool = False
    channel_plan: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    model_name: str
    model_code: int
    firmware: str


@dataclass
class Session:
    """Everything saved. ``traces`` (live/max/avg/min/scan/ref1..) is not part of equality: it
    lives in ``traces.npz`` and numpy arrays have no single truth value."""

    settings: SessionSettings = field(default_factory=SessionSettings)
    traces: dict[str, Trace] = field(default_factory=dict, compare=False)
    markers: list[Marker] = field(default_factory=list)
    exclusion_zones: list[ExclusionZone] = field(default_factory=list)
    #: The solved frequency plan (``coordination_model.result_to_dict``), ``None`` if none.
    plan: dict[str, Any] | None = None
    #: The coordination setup: device rows, locked carriers, options (``CoordinationModel``).
    coordination: dict[str, Any] | None = None
    device: DeviceInfo | None = None
    created: str = ""
    modified: str = ""
    opencoord_version: str = ""


# --- JSON -------------------------------------------------------------------------------------


def to_json(session: Session) -> str:
    s = session.settings
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "opencoord_version": session.opencoord_version,
        "created": session.created,
        "modified": session.modified,
        "device": None
        if session.device is None
        else {
            "model_name": session.device.model_name,
            "model_code": session.device.model_code,
            "firmware": session.device.firmware,
        },
        "settings": {
            "start_hz": s.start_hz,
            "stop_hz": s.stop_hz,
            "mode": s.mode,
            "preset": s.preset,
            "resolution": s.resolution,
            "threshold_dbm": s.threshold_dbm,
            "overlay_enabled": s.overlay_enabled,
            "channel_plan": s.channel_plan,
        },
        "traces": {name: {"label": t.label} for name, t in session.traces.items()},
        "markers": [
            {"id": m.id, "freq_hz": m.freq_hz, "trace_key": m.trace_key} for m in session.markers
        ],
        "exclusion_zones": [
            {"id": z.id, "start_hz": z.start_hz, "stop_hz": z.stop_hz}
            for z in session.exclusion_zones
        ],
        "plan": session.plan,
        "coordination": session.coordination,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _get(doc: Mapping[str, Any], key: str, kind: type | tuple[type, ...], default: Any) -> Any:
    value = doc.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise SessionError(f"Invalid session: '{key}' has the wrong type")
    if not isinstance(value, kind):
        raise SessionError(f"Invalid session: '{key}' has the wrong type")
    return value


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _records(doc: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    items: list[Any] = _get(doc, key, list, [])
    if not all(isinstance(i, dict) for i in items):
        raise SessionError(f"Invalid session: '{key}' must be a list of objects")
    return items


def _int(rec: Mapping[str, Any], key: str) -> int:
    value = rec.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionError(f"Invalid session: '{key}' must be an integer")
    return value


def from_json(text: str, arrays: Arrays | None = None) -> Session:
    """Parse ``session.json``; ``arrays`` (from :func:`decode_traces`) supplies the trace data."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise SessionError(f"The session file is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SessionError("Invalid session: expected a JSON object")
    version = doc.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SessionError("Invalid session: 'schema_version' is missing")
    if version > SCHEMA_VERSION:
        raise SessionError(
            f"This session uses format version {version}, which is newer than this OpenCoord "
            f"understands ({SCHEMA_VERSION}); update OpenCoord to open it"
        )
    if version < 1:
        raise SessionError(f"Unsupported session format version {version}")

    d = SessionSettings()
    raw = _get(doc, "settings", dict, {})
    settings = SessionSettings(
        start_hz=_get(raw, "start_hz", int, d.start_hz),
        stop_hz=_get(raw, "stop_hz", int, d.stop_hz),
        mode=_get(raw, "mode", str, d.mode),
        preset=_get(raw, "preset", str, d.preset),
        resolution=_get(raw, "resolution", str, d.resolution),
        threshold_dbm=_finite_or_none(_get(raw, "threshold_dbm", (int, float), None)),
        overlay_enabled=_get(raw, "overlay_enabled", bool, d.overlay_enabled),
        channel_plan=_get(raw, "channel_plan", str, d.channel_plan),
    )
    device_raw = _get(doc, "device", dict, None)
    device = (
        None
        if device_raw is None
        else DeviceInfo(
            _get(device_raw, "model_name", str, ""),
            _get(device_raw, "model_code", int, 0),
            _get(device_raw, "firmware", str, ""),
        )
    )
    labels = {
        name: _get(meta, "label", str, name)
        for name, meta in _get(doc, "traces", dict, {}).items()
        if isinstance(meta, dict)
    }
    return Session(
        settings=settings,
        traces=build_traces(labels, arrays or {}),
        markers=[
            Marker(_int(m, "id"), _int(m, "freq_hz"), _get(m, "trace_key", str, "max"))
            for m in _records(doc, "markers")
        ],
        exclusion_zones=[
            ExclusionZone(_int(z, "id"), _int(z, "start_hz"), _int(z, "stop_hz"))
            for z in _records(doc, "exclusion_zones")
        ],
        plan=_get(doc, "plan", dict, None),
        coordination=_get(doc, "coordination", dict, None),
        device=device,
        created=_get(doc, "created", str, ""),
        modified=_get(doc, "modified", str, ""),
        opencoord_version=_get(doc, "opencoord_version", str, ""),
    )


# --- traces.npz ---------------------------------------------------------------------------------


def encode_traces(traces: Mapping[str, Trace]) -> bytes:
    arrays: dict[str, npt.NDArray[Any]] = {}
    for name, t in traces.items():
        arrays[f"{name}.freqs_hz"] = np.asarray(t.freqs_hz, dtype=np.float64)
        arrays[f"{name}.dbm"] = np.asarray(t.dbm, dtype=np.float32)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)  # type: ignore[arg-type]
    return buf.getvalue()


def decode_traces(data: bytes) -> Arrays:
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            if sum(i.file_size for i in npz.zip.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise SessionError("The session's trace data is too large")
            keys = set(npz.files)
            out: Arrays = {}
            for key in sorted(keys):
                name, _, kind = key.rpartition(".")
                if kind == "freqs_hz" and f"{name}.dbm" in keys:
                    out[name] = (
                        np.asarray(npz[key], dtype=np.float64),
                        np.asarray(npz[f"{name}.dbm"], dtype=np.float32),
                    )
            return out
    except _READ_ERRORS as exc:
        raise SessionError(f"The session's trace data is unreadable: {exc}") from exc


def build_traces(labels: Mapping[str, str], arrays: Arrays) -> dict[str, Trace]:
    """Traces named in ``labels`` that have arrays; mismatched or non-finite data is an error."""
    out: dict[str, Trace] = {}
    for name, label in labels.items():
        if name not in arrays:
            continue
        freqs, dbm = arrays[name]
        if freqs.ndim != 1 or freqs.shape != dbm.shape or len(freqs) == 0:
            raise SessionError(f"Invalid session: trace '{name}' has inconsistent data")
        if not (np.isfinite(freqs).all() and np.isfinite(dbm).all()):
            raise SessionError(f"Invalid session: trace '{name}' has non-finite values")
        out[name] = Trace(freqs, dbm, label)
    return out


# --- file I/O -----------------------------------------------------------------------------------


def save(path: Path, session: Session) -> None:
    """Write ``session`` to ``path`` atomically."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(_JSON_NAME, to_json(session))
        z.writestr(_NPZ_NAME, encode_traces(session.traces))
    write_atomic(path, buf.getvalue())


def load(path: Path) -> Session:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if _JSON_NAME not in names:
                raise SessionError(f"Not an OpenCoord session: {_JSON_NAME} is missing")
            if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise SessionError(f"{path.name} is too large to be a session")
            text = z.read(_JSON_NAME).decode("utf-8")
            blob = z.read(_NPZ_NAME) if _NPZ_NAME in names else None
    except FileNotFoundError as exc:
        raise SessionError(f"File not found: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Cannot read {path.name}: {exc.strerror or exc}") from exc
    except (*_READ_ERRORS, UnicodeDecodeError) as exc:
        raise SessionError(f"Not a readable OpenCoord session file: {path.name} ({exc})") from exc
    return from_json(text, decode_traces(blob) if blob is not None else None)
