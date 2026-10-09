"""RF Explorer model capability table (pure; no I/O).

Model codes: RFExplorer-for-Python ``RFE_Common.eModel``
(github.com/RFExplorer/RFExplorer-for-Python), fetched 2026-10-09: 433M=0, 868M=1, 915M=2,
WSUB1G=3, 2.4G=4, WSUB3G=5, 6G=6, WSUB1G+=10, AudioPro=11, 2400+=12, 4G+=13, 6G+=14,
W5G3G=16, W5G4G=17,
W5G5G=18, RFGen=60, RFGen expansion=61, none=0xFF.
Only code 10 has been verified on hardware.

Frequency ranges are *hints* taken from the official RF Explorer model specifications
(j3.rf-explorer.com / rf-explorer.com product pages) and, for the generators, from the
``CONST_RFGEN*_FREQ_MHZ`` constants in ``RFE_Common``. The ranges for models other than the WSUB1G+
(verified by its ``#C2-F`` reply) were NOT re-verified against a device. The live ``#C2-F`` config
always wins over these hints; see ``resolve``.
"""

from __future__ import annotations

from dataclasses import dataclass

from opencoord.core.types import DeviceConfig, ModelInfo

_KHZ = 1_000
_MHZ = 1_000_000

_LEGACY_POINTS = 112
_PLUS_POINTS = 4096


@dataclass(frozen=True)
class Capabilities:
    """Capabilities of the active module (``name``); main and expansion names are both exposed."""

    name: str
    min_hz: int
    max_hz: int
    max_span_hz: int
    is_plus: bool
    expansion: bool
    sweep_points_max: int
    main_name: str
    expansion_name: str | None


@dataclass(frozen=True)
class ModelHint:
    """Table entry for one model code."""

    name: str
    min_hz: int
    max_hz: int
    max_span_hz: int
    is_plus: bool
    sweep_points_max: int


def _hint(name: str, lo: int, hi: int, *, plus: bool = False, span: int | None = None) -> ModelHint:
    return ModelHint(
        name=name,
        min_hz=lo,
        max_hz=hi,
        max_span_hz=hi - lo if span is None else span,
        is_plus=plus,
        sweep_points_max=_PLUS_POINTS if plus else _LEGACY_POINTS,
    )


MODELS: dict[int, ModelHint] = {
    0: _hint("RF Explorer 433M", 240 * _MHZ, 480 * _MHZ),
    1: _hint("RF Explorer 868M", 430 * _MHZ, 870 * _MHZ),
    2: _hint("RF Explorer 915M", 450 * _MHZ, 930 * _MHZ),
    3: _hint("RF Explorer WSUB1G", 50 * _MHZ, 960 * _MHZ),
    4: _hint("RF Explorer 2.4G", 2400 * _MHZ, 2500 * _MHZ),
    5: _hint("RF Explorer WSUB3G", 15 * _MHZ, 2700 * _MHZ),
    6: _hint("RF Explorer 6G", 4850 * _MHZ, 6100 * _MHZ),
    10: _hint("RF Explorer WSUB1G+", 50 * _KHZ, 960 * _MHZ, plus=True),
    11: _hint("RF Explorer AudioPro", 50 * _KHZ, 960 * _MHZ, plus=True),
    12: _hint("RF Explorer 2400+", 2400 * _MHZ, 2500 * _MHZ, plus=True),
    13: _hint("RF Explorer 4G+", 240 * _MHZ, 4000 * _MHZ, plus=True),
    14: _hint("RF Explorer 6G+", 240 * _MHZ, 6100 * _MHZ, plus=True),
    16: _hint("RF Explorer W5G3G", 15 * _MHZ, 2700 * _MHZ, plus=True),
    17: _hint("RF Explorer W5G4G", 240 * _MHZ, 4000 * _MHZ, plus=True),
    18: _hint("RF Explorer W5G5G", 4850 * _MHZ, 6100 * _MHZ, plus=True),
    # RFE_Common CONST_RFGEN_MIN/MAX_FREQ_MHZ = 23.438 / 6000, CONST_RFGENEXP_* = 0.100 / 6000
    60: _hint("RF Explorer RF Generator", 23_438_000, 6000 * _MHZ),
    61: _hint("RF Explorer RF Generator expansion", 100 * _KHZ, 6000 * _MHZ),
}


def _name(code: int | None) -> str | None:
    if code is None:
        return None
    hint = MODELS.get(code)
    return hint.name if hint is not None else f"Unknown model (code {code})"


def resolve(model: ModelInfo, config: DeviceConfig | None) -> Capabilities:
    """Capabilities of the *active* module; values in ``config`` win over table hints.

    The expansion module's hints apply only when ``config.expansion_active`` is true. Unknown codes
    are named ``Unknown model (code N)`` and use the config limits (zeros when there is no config).
    """
    expansion = bool(
        config is not None and config.expansion_active and model.expansion_code is not None
    )
    code = model.expansion_code if expansion else model.main_code
    assert code is not None
    hint = MODELS.get(code)
    name = _name(code)
    assert name is not None
    main_name = _name(model.main_code)
    assert main_name is not None

    min_hz = hint.min_hz if hint else 0
    max_hz = hint.max_hz if hint else 0
    max_span_hz = hint.max_span_hz if hint else 0
    if config is not None:
        min_hz, max_hz, max_span_hz = config.min_hz, config.max_hz, config.max_span_hz

    return Capabilities(
        name=name,
        min_hz=min_hz,
        max_hz=max_hz,
        max_span_hz=max_span_hz,
        is_plus=hint.is_plus if hint else False,
        expansion=expansion,
        sweep_points_max=hint.sweep_points_max if hint else (config.sweep_points if config else 0),
        main_name=main_name,
        expansion_name=_name(model.expansion_code),
    )
