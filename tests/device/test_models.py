from __future__ import annotations

import dataclasses

from opencoord.core.types import DeviceConfig, ModelInfo
from opencoord.device.models import MODELS, resolve


def _config(**over: object) -> DeviceConfig:
    base = DeviceConfig(
        start_hz=470_000_000,
        step_hz=1_000_000,
        amp_top_dbm=-10.0,
        amp_bottom_dbm=-120.0,
        sweep_points=512,
        expansion_active=False,
        mode=0,
        min_hz=50_000,
        max_hz=960_000_000,
        max_span_hz=342_370_000,
        rbw_hz=None,
        amp_offset_db=None,
        calculator_mode=None,
    )
    return dataclasses.replace(base, **over)  # type: ignore[arg-type]


def test_known_codes_table() -> None:
    expected = {0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 16, 17, 18, 60, 61}
    assert set(MODELS) == expected
    assert MODELS[10].name == "RF Explorer WSUB1G+"
    assert MODELS[10].is_plus
    assert MODELS[10].min_hz == 50_000
    assert MODELS[10].max_hz == 960_000_000
    assert not MODELS[3].is_plus


def test_hints_without_config() -> None:
    caps = resolve(ModelInfo(10, None, "01.12"), None)
    assert caps.name == "RF Explorer WSUB1G+"
    assert caps.main_name == "RF Explorer WSUB1G+"
    assert caps.expansion_name is None
    assert caps.max_hz == 960_000_000
    assert not caps.expansion


def test_config_wins() -> None:
    caps = resolve(ModelInfo(10, None, "01.12"), _config(min_hz=100_000, max_hz=900_000_000))
    assert caps.min_hz == 100_000
    assert caps.max_hz == 900_000_000
    assert caps.max_span_hz == 342_370_000
    assert caps.is_plus


def test_unknown_code_uses_config() -> None:
    caps = resolve(ModelInfo(99, None, "01.00"), _config())
    assert caps.name == "Unknown model (code 99)"
    assert caps.min_hz == 50_000
    assert caps.max_hz == 960_000_000
    assert caps.max_span_hz == 342_370_000
    assert not caps.is_plus


def test_unknown_code_without_config() -> None:
    caps = resolve(ModelInfo(99, None, "01.00"), None)
    assert caps.name == "Unknown model (code 99)"
    assert caps.max_hz == 0


def test_expansion_active_uses_expansion_table() -> None:
    model = ModelInfo(10, 6, "01.12")
    caps = resolve(
        model, _config(expansion_active=True, min_hz=4_850_000_000, max_hz=6_100_000_000)
    )
    assert caps.expansion
    assert caps.name == MODELS[6].name
    assert caps.main_name == MODELS[10].name
    assert caps.expansion_name == MODELS[6].name
    assert caps.min_hz == 4_850_000_000


def test_expansion_present_but_inactive_uses_main() -> None:
    caps = resolve(ModelInfo(10, 6, "01.12"), _config())
    assert not caps.expansion
    assert caps.name == MODELS[10].name
    assert caps.expansion_name == MODELS[6].name


def test_expansion_active_hint_without_config() -> None:
    # config absent: expansion_active unknown -> main module
    caps = resolve(ModelInfo(10, 6, "01.12"), None)
    assert caps.name == MODELS[10].name
