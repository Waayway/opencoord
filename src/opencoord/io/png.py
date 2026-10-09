"""Minimal PNG encoder (pure: ``zlib`` + ``struct``, 8-bit RGBA, no Pillow)."""

from __future__ import annotations

import struct
import zlib

import numpy as np
import numpy.typing as npt

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def encode_png(rgba: npt.NDArray[np.uint8]) -> bytes:
    """PNG file bytes for ``rgba``, an ``(height, width, 4)`` ``uint8`` array (no filtering)."""
    if rgba.dtype != np.uint8 or rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("expected a (height, width, 4) uint8 RGBA array")
    height, width = int(rgba.shape[0]), int(rgba.shape[1])
    if height == 0 or width == 0:
        raise ValueError("the image is empty")
    rows = np.zeros((height, width * 4 + 1), dtype=np.uint8)  # column 0 = filter type 0 (None)
    rows[:, 1:] = rgba.reshape(height, width * 4)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        _SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
        + _chunk(b"IEND", b"")
    )
