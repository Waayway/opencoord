import dataclasses

import numpy as np
import pytest

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep


def _config(**overrides: object) -> DeviceConfig:
    values: dict[str, object] = {
        "start_hz": 431_000_000,
        "step_hz": 90_090,
        "amp_top_dbm": -10.0,
        "amp_bottom_dbm": -120.0,
        "sweep_points": 112,
        "expansion_active": False,
        "mode": 0,
        "min_hz": 50_000,
        "max_hz": 960_000_000,
        "max_span_hz": 959_950_000,
        "rbw_hz": 110_000,
        "amp_offset_db": 0.0,
        "calculator_mode": 4,
    }
    values.update(overrides)
    return DeviceConfig(**values)  # type: ignore[arg-type]


def test_sweep_start_and_stop_are_int_hz() -> None:
    sweep = Sweep(
        freqs_hz=np.array([470e6, 470.5e6, 471e6]),
        dbm=np.array([-100.0, -90.0, -95.0], dtype=np.float32),
        timestamp=12.5,
    )
    assert sweep.start_hz == 470_000_000
    assert sweep.stop_hz == 471_000_000
    assert isinstance(sweep.start_hz, int)
    assert isinstance(sweep.stop_hz, int)


def test_sweep_is_frozen() -> None:
    sweep = Sweep(freqs_hz=np.array([1.0]), dbm=np.array([-1.0], dtype=np.float32), timestamp=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sweep.timestamp = 1.0  # type: ignore[misc]


def test_device_config_stop_hz_is_last_sample_frequency() -> None:
    config = _config()
    assert config.stop_hz == 431_000_000 + 111 * 90_090


def test_device_config_is_frozen_and_hashable() -> None:
    config = _config()
    assert config == _config()
    assert hash(config) == hash(_config())
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.start_hz = 0  # type: ignore[misc]


def test_model_info_fields() -> None:
    info = ModelInfo(main_code=10, expansion_code=None, firmware="03.39")
    assert info.main_code == 10
    assert info.expansion_code is None
    assert info.firmware == "03.39"
