"""Amplitude offset helpers (pure)."""

from __future__ import annotations

import numpy as np
import pytest

from opencoord.core.offsets import offset_key, offset_sweep, offset_trace
from opencoord.core.types import DeviceConfig, ModelInfo, Sweep, Trace


def _config(expansion: bool) -> DeviceConfig:
    return DeviceConfig(
        start_hz=0, step_hz=1, amp_top_dbm=0, amp_bottom_dbm=-100, sweep_points=2,
        expansion_active=expansion, mode=0, min_hz=0, max_hz=1, max_span_hz=1,
        rbw_hz=None, amp_offset_db=None, calculator_mode=None,
    )  # fmt: skip


def test_key_follows_the_active_module() -> None:
    model = ModelInfo(10, 4, "03.39")
    assert offset_key(None, None) is None
    assert offset_key(model, None) == "model_10"
    assert offset_key(model, _config(False)) == "model_10"
    assert offset_key(model, _config(True)) == "model_4"
    assert offset_key(ModelInfo(10, None, "x"), _config(True)) == "model_10"


def test_sweep_and_trace_are_shifted_not_mutated() -> None:
    f = np.array([1.0, 2.0])
    d = np.array([-80.0, -70.0], dtype=np.float32)
    s = offset_sweep(Sweep(f, d, 1.0), 6.0)
    assert s.dbm.tolist() == pytest.approx([-74.0, -64.0]) and d[0] == -80.0
    t = offset_trace(Trace(f, d, "t"), -2.0)
    assert t is not None and t.dbm.tolist() == pytest.approx([-82.0, -72.0])


def test_zero_offset_returns_the_same_object() -> None:
    sw = Sweep(np.array([1.0]), np.array([-80.0], dtype=np.float32), 0.0)
    assert offset_sweep(sw, 0.0) is sw
    assert offset_trace(None, 5.0) is None
