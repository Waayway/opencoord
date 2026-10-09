"""SerialLink against a real RF Explorer (``OPENCOORD_HARDWARE=1``, optional ``OPENCOORD_PORT``)."""

from __future__ import annotations

import os
import queue
import time

import pytest

from opencoord.core.types import Sweep
from opencoord.device.link import SerialLink

MHZ = 1_000_000

pytestmark = pytest.mark.hardware


def _sweeps(link: SerialLink, n: int, timeout_s: float, start_hz: int, stop_hz: int) -> list[Sweep]:
    """Up to ``n`` sweeps lying within ``start_hz..stop_hz``; others (old config) are skipped."""
    got: list[Sweep] = []
    deadline = time.monotonic() + timeout_s
    while len(got) < n and time.monotonic() < deadline:
        try:
            sweep = link.sweeps.get(timeout=0.2)
        except queue.Empty:
            continue
        if start_hz <= sweep.start_hz and sweep.stop_hz <= stop_hz:
            got.append(sweep)
    return got


def _wait_config(link: SerialLink, start_hz: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if link.config is not None and abs(link.config.start_hz - start_hz) < MHZ:
            return
        time.sleep(0.05)
    raise AssertionError(f"device config did not move to {start_hz} Hz: {link.config}")


def test_real_device_streams_and_retunes() -> None:
    link = SerialLink(os.environ.get("OPENCOORD_PORT"))
    link.open()
    original = link.config
    try:
        assert link.model is not None and link.capabilities is not None
        assert original is not None
        print(f"\nmodel {link.model}, {link.capabilities.name}\nconfig {original}")
        assert len(_sweeps(link, 3, 10.0, original.start_hz, original.stop_hz)) >= 3

        link.set_span(470 * MHZ, 700 * MHZ)
        _wait_config(link, 470 * MHZ)
        retuned = _sweeps(link, 3, 15.0, 470 * MHZ, 700 * MHZ)
        assert len(retuned) >= 3
        for sweep in retuned:
            assert (
                abs(sweep.start_hz - 470 * MHZ) < MHZ and abs(sweep.stop_hz - 700 * MHZ) < 3 * MHZ
            )
            assert float(sweep.dbm.max()) < 0 and float(sweep.dbm.min()) > -140
        print(f"retuned {link.config}\nfirst sweep {retuned[0].start_hz}..{retuned[0].stop_hz} Hz")
    finally:
        # Leave the device as we found it (span rounded to whole kHz, amplitudes kept by set_span).
        if original is not None and link.is_open:
            link.set_span(original.start_hz, round(original.stop_hz / 1000) * 1000)
            _wait_config(link, original.start_hz)
        link.close()
