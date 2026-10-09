"""Amplitude offset helpers (pure): which settings key a module uses and how it is applied."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep, Trace


def active_model_code(model: ModelInfo | None, config: DeviceConfig | None) -> int | None:
    """Model code of the active module (the expansion's when it is active); ``None`` without a
    model."""
    if model is None:
        return None
    if config is not None and config.expansion_active and model.expansion_code is not None:
        return model.expansion_code
    return model.main_code


def offset_key(model: ModelInfo | None, config: DeviceConfig | None) -> str | None:
    """Settings key ``model_<code>`` of the active module; ``None`` before a device reports.

    The link exposes no serial number, so offsets are per model code, not per unit.
    """
    code = active_model_code(model, config)
    return None if code is None else f"model_{code}"


def offset_levels(dbm: npt.NDArray[np.float32], offset_db: float) -> npt.NDArray[np.float32]:
    return dbm + np.float32(offset_db)


def offset_sweep(sweep: Sweep, offset_db: float) -> Sweep:
    if offset_db == 0.0:
        return sweep
    return Sweep(sweep.freqs_hz, offset_levels(sweep.dbm, offset_db), sweep.timestamp)


def offset_trace(trace: Trace | None, offset_db: float) -> Trace | None:
    if trace is None or offset_db == 0.0:
        return trace
    return Trace(trace.freqs_hz, offset_levels(trace.dbm, offset_db), trace.label)
