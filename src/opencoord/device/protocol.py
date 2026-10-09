"""RF Explorer serial protocol: command builders and an incremental parser.

Pure module: no serial access, no file I/O. ``SerialLink`` (``device/link.py``) writes the bytes
built here and feeds whatever it reads into :class:`Parser`.

References (formats re-implemented, no code copied):

* RF Explorer UART API interface specification,
  https://github.com/RFExplorer/RFExplorer-for-.NET/wiki/RF-Explorer-UART-API-interface-specification
* RFExplorer-for-Python (LGPL-3.0), ``RFExplorer/ReceiveSerialThread.py`` and ``RFExplorer.py``,
  https://github.com/RFExplorer/RFExplorer-for-Python

Where these disagree, the bytes recorded from a WSUB1G PLUS (``tests/fixtures/wsub1gplus_*.bin``)
win. Formats not yet seen on real hardware are marked with ``⚠``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep

log = logging.getLogger(__name__)

#: "Early End Of Transmission": the device sends this when it aborts a sweep dump, e.g. after a
#: reconfiguration or when the previous session closed the port mid-sweep (seen in every fixture).
EEOT = b"\xff\xfe\xff\xfe\x00"

_NO_EXPANSION = 255
_MAX_LINE = 512  # longest text line we wait for; real lines are < 100 bytes
_CRLF = b"\r\n"


# --- events -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelReply:
    """``#C2-M`` line: model codes and firmware version."""

    model: ModelInfo


@dataclass(frozen=True)
class ConfigReply:
    """``#C2-F`` line: current analyzer configuration."""

    config: DeviceConfig


@dataclass(frozen=True, eq=False)
class SweepData:
    """``$S`` / ``$s`` / ``$z`` frame: one sweep's samples in dBm (no frequency axis yet)."""

    samples: npt.NDArray[np.float32]


@dataclass(frozen=True)
class Unknown:
    """A complete text line we do not interpret (``#Sn…``, ``#CAL:…``, the banner, …)."""

    line: str


@dataclass(frozen=True)
class ParseError:
    """Bytes the parser dropped, and why. Never raised; returned for logging and tests."""

    reason: str
    data: bytes


Event: TypeAlias = ModelReply | ConfigReply | SweepData | Unknown | ParseError


# --- command builders ---------------------------------------------------------------------------


def _command(payload: bytes) -> bytes:
    """Frame a command: ``#`` + total length (including ``#`` and the length byte) + payload."""
    return b"#" + bytes([len(payload) + 2]) + payload


def request_config() -> bytes:
    """``C0``: ask for ``#C2-M`` + ``#C2-F`` (and start sweep dumps)."""
    return _command(b"C0")


def hold() -> bytes:
    """``CH``: stop the sweep data dump. ⚠ not yet sent to real hardware."""
    return _command(b"CH")


def switch_module(main: bool) -> bytes:
    """``CM`` + binary 0 (main board) or 1 (expansion). ⚠ needs a unit with an expansion module."""
    return _command(b"CM" + (b"\x00" if main else b"\x01"))


def _amp_field(dbm: int) -> bytes:
    if not -999 <= dbm <= 9999:
        raise ValueError(f"amplitude {dbm} dBm does not fit in 4 characters")
    return f"{dbm:04d}".encode("ascii")  # -10 -> "-010", 5 -> "0005", as the device reports them


def set_config(start_hz: int, stop_hz: int, top_dbm: int, bottom_dbm: int) -> bytes:
    """``C2-F:SSSSSSS,EEEEEEE,TTTT,BBBB``: set the span (kHz, 7 digits) and the amplitude range.

    Frequencies are truncated to whole kHz. The device answers with a new ``#C2-F``.
    """
    start_khz, stop_khz = start_hz // 1000, stop_hz // 1000
    if not 0 <= start_khz < stop_khz <= 9_999_999:
        raise ValueError(f"invalid span {start_hz}..{stop_hz} Hz")
    if top_dbm <= bottom_dbm:
        raise ValueError(f"top {top_dbm} dBm must be above bottom {bottom_dbm} dBm")
    payload = b"C2-F:%07d,%07d,%s,%s" % (
        start_khz,
        stop_khz,
        _amp_field(top_dbm),
        _amp_field(bottom_dbm),
    )
    return _command(payload)


def set_sweep_points(n: int) -> bytes:
    """Set the number of sweep points.

    Multiples of 16 up to 4096 use ``CJ`` + one byte ``(n - 16) / 16``; anything else in
    112..65535 uses ``Cj`` + a big-endian 16-bit count (Plus/3G firmware only).
    """
    if 16 <= n <= 4096 and n % 16 == 0:
        return _command(b"CJ" + bytes([(n - 16) // 16]))
    if 112 <= n <= 0xFFFF:
        return _command(b"Cj" + n.to_bytes(2, "big"))
    raise ValueError(f"unsupported sweep point count {n}")


# --- reply parsing ------------------------------------------------------------------------------


def _fields(line: str, prefix_len: int) -> list[str]:
    return [f.strip() for f in line[prefix_len:].split(",")]


def _parse_model(line: str) -> ModelInfo:
    main, expansion, firmware = _fields(line, 6)[:3]
    exp_code = int(expansion)
    return ModelInfo(
        main_code=int(main),
        expansion_code=None if exp_code == _NO_EXPANSION else exp_code,
        firmware=firmware,
    )


def _parse_config(line: str) -> DeviceConfig:
    f = _fields(line, 6)
    if len(f) < 10:
        raise ValueError(f"expected at least 10 fields, got {len(f)}")
    # Field widths vary (step can be 8 digits, points 5), so split on commas instead of slicing.
    return DeviceConfig(
        start_hz=int(f[0]) * 1000,
        step_hz=int(f[1]),
        amp_top_dbm=float(int(f[2])),
        amp_bottom_dbm=float(int(f[3])),
        sweep_points=int(f[4]),
        expansion_active=f[5] == "1",
        mode=int(f[6]),
        min_hz=int(f[7]) * 1000,
        max_hz=int(f[8]) * 1000,
        max_span_hz=int(f[9]) * 1000,
        rbw_hz=int(f[10]) * 1000 if len(f) > 10 else None,
        amp_offset_db=float(int(f[11])) if len(f) > 11 else None,
        calculator_mode=int(f[12]) if len(f) > 12 else None,
    )


def _text_line(line: str) -> Event:
    try:
        if line.startswith("#C2-M:"):
            return ModelReply(_parse_model(line))
        if line.startswith(("#C2-F:", "#C2-f:")):
            return ConfigReply(_parse_config(line))
    except ValueError as exc:
        return ParseError(f"malformed {line[:5]} line: {exc}", line.encode("ascii"))
    return Unknown(line)


def _is_text(data: bytes) -> bool:
    return all(0x20 <= b < 0x7F for b in data)


def _samples(raw: bytes) -> npt.NDArray[np.float32]:
    # Each sample byte is unsigned; dBm = -byte / 2.
    return np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / np.float32(-2.0)


# A step returns (events, bytes consumed); None means "need more data".
_Step: TypeAlias = tuple[list[Event], int] | None


class Parser:
    """Incremental, stateful decoder for the device → host byte stream.

    ``feed`` accepts any chunking of the stream and never raises: undecodable bytes are dropped
    and reported as :class:`ParseError`, then the parser resynchronises at the next ``#`` or ``$``.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # Point count from the last #C2-F. The device always sends #C2-F before sweeps of a new
        # size (F2), so a sweep header with any other count is garbage.
        self._sweep_points: int | None = None

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while self._buf:
            step = self._step(bytes(self._buf))
            if step is None:
                break
            new_events, consumed = step
            events.extend(new_events)
            for event in new_events:
                if isinstance(event, ConfigReply):
                    self._sweep_points = event.config.sweep_points
            del self._buf[:consumed]
        for event in events:
            if isinstance(event, ParseError):
                log.debug("dropped %d bytes: %s", len(event.data), event.reason)
        return events

    def _step(self, buf: bytes) -> _Step:
        if buf[:1] == EEOT[:1] and EEOT.startswith(buf[: len(EEOT)]):
            if len(buf) < len(EEOT):
                return None
            return [ParseError("early end of transmission (EEOT)", EEOT)], len(EEOT)
        if buf[:1] == b"#":
            return self._hash_line(buf)
        if buf[:1] == b"$":
            return self._sweep(buf)
        return self._other(buf)

    def _hash_line(self, buf: bytes) -> _Step:
        end = buf.find(_CRLF, 0, _MAX_LINE)
        if end < 0:
            if len(buf) < _MAX_LINE:
                return None
            return self._garbage(buf, "line too long without CR LF")
        line = buf[:end]
        if not _is_text(line):
            return self._garbage(buf, "binary data in # line")
        return [_text_line(line.decode("ascii"))], end + 2

    def _sweep(self, buf: bytes) -> _Step:
        if len(buf) < 3:
            return None
        kind = buf[1:2]
        if kind == b"S":
            header, count = 3, buf[2]
        elif kind == b"s":
            # ⚠ Count byte x16, 0 meaning 4096 (RFExplorer-for-Python); not seen on the WSUB1G+.
            header, count = 3, (buf[2] or 256) * 16
        elif kind == b"z":
            if len(buf) < 4:
                return None
            header, count = 4, int.from_bytes(buf[2:4], "big")
        else:
            return self._garbage(buf, f"unsupported frame ${kind.decode('latin-1')}")
        if count == 0:
            return self._garbage(buf, "sweep frame with zero points")
        if self._sweep_points is not None and count != self._sweep_points:
            return self._garbage(
                buf, f"sweep frame of {count} points, config says {self._sweep_points}"
            )
        total = header + count + len(_CRLF)
        if len(buf) < total:
            return self._aborted(buf, header, len(buf))
        if buf[total - 2 : total] != _CRLF:
            return self._aborted(buf, header, total) or self._garbage(
                buf, "sweep frame not terminated by CR LF"
            )
        return [SweepData(_samples(buf[header : header + count]))], total

    @staticmethod
    def _aborted(buf: bytes, header: int, end: int) -> _Step:
        """Drop a pending sweep that the device cut short with EEOT; None if it was not.

        EEOT only counts if it comes before the next ``#``/``$``: if the header was spurious, a
        real frame may follow it, and that must not be swallowed. (A real payload containing
        0x23/0x24 then waits for its full length and is resynced instead; nothing is lost.)
        """
        limit = min([end] + [i for i in (buf.find(b"#", header), buf.find(b"$", header)) if i >= 0])
        eeot = buf.find(EEOT, header, limit)
        if eeot < 0:
            return None
        stop = eeot + len(EEOT)
        return [ParseError("sweep aborted by device (EEOT)", buf[:stop])], stop

    def _other(self, buf: bytes) -> _Step:
        # Plain text lines (e.g. the "RF Explorer 03.39 ..." banner sent in reply to C0).
        n = 0
        while n < len(buf) and n < _MAX_LINE and 0x20 <= buf[n] < 0x7F:
            n += 1
        if buf[n : n + 2] == _CRLF:
            if n == 0:
                return [], 2  # empty line
            return [Unknown(buf[:n].decode("ascii"))], n + 2
        if n < _MAX_LINE and buf[n:] in (b"", b"\r"):
            return None
        return self._garbage(buf, "unexpected bytes")

    @staticmethod
    def _garbage(buf: bytes, reason: str) -> _Step:
        """Drop bytes up to the next ``#``/``$`` after index 0 (so the parser always advances)."""
        candidates = [i for i in (buf.find(b"#", 1), buf.find(b"$", 1)) if i > 0]
        skip = min(candidates) if candidates else len(buf)
        return [ParseError(reason, buf[:skip])], skip


def make_sweep(config: DeviceConfig, samples: npt.NDArray[np.float32], timestamp: float) -> Sweep:
    """Combine a :class:`SweepData` payload with the config it was taken under.

    Point ``i`` is at ``start_hz + i * step_hz``. The device's own amplitude offset
    (``#C2-F`` AmpOffset) is added, as RFExplorer-for-Python does (⚠ only seen as 0 so far).
    Raises ``ValueError`` if the sample count does not match ``config.sweep_points``.
    """
    if samples.shape != (config.sweep_points,):
        raise ValueError(
            f"sweep has {samples.shape[0]} points, config expects {config.sweep_points}"
        )
    freqs = config.start_hz + np.arange(config.sweep_points, dtype=np.float64) * config.step_hz
    dbm = samples.astype(np.float32) + np.float32(config.amp_offset_db or 0.0)
    return Sweep(freqs_hz=freqs, dbm=dbm, timestamp=timestamp)
