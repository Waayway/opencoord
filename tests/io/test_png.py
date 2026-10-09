import struct
import zlib

import numpy as np
import pytest

from opencoord.io.png import encode_png


def decode(data: bytes) -> np.ndarray:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", (0, 0)
    chunks = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body)
        chunks.append(kind)
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", body)
            assert (depth, ctype) == (8, 6)
            size = (h, w)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    assert chunks[0] == b"IHDR" and chunks[-1] == b"IEND"
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(size[0], size[1] * 4 + 1)
    assert (raw[:, 0] == 0).all()
    return raw[:, 1:].reshape(size[0], size[1], 4)


def test_round_trip() -> None:
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(7, 13, 4), dtype=np.uint8)
    assert np.array_equal(decode(encode_png(img)), img)


def test_one_pixel() -> None:
    img = np.array([[[1, 2, 3, 4]]], dtype=np.uint8)
    assert np.array_equal(decode(encode_png(img)), img)


@pytest.mark.parametrize(
    "shape,dtype", [((4, 4, 3), np.uint8), ((4, 4, 4), np.float32), ((0, 4, 4), np.uint8)]
)
def test_rejects_bad_input(shape: tuple[int, ...], dtype: type) -> None:
    with pytest.raises(ValueError):
        encode_png(np.zeros(shape, dtype=dtype))
