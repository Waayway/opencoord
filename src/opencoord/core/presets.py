"""Built-in frequency range presets from plan §4 (pure; no I/O).

Names use plain ASCII hyphens so they render in the default UI font.
"""

from __future__ import annotations

from dataclasses import dataclass

_MHZ = 1_000_000

#: Name of the preset covering the connected device's full range.
OVERVIEW = "Overview"


@dataclass(frozen=True)
class RangePreset:
    name: str
    start_hz: int
    stop_hz: int


PRESETS: tuple[RangePreset, ...] = (
    RangePreset("Full UHF 470-960", 470 * _MHZ, 960 * _MHZ),
    RangePreset("TV 21-48 (470-694)", 470 * _MHZ, 694 * _MHZ),
    RangePreset("694-790", 694 * _MHZ, 790 * _MHZ),
    RangePreset("823-832", 823 * _MHZ, 832 * _MHZ),
    RangePreset("863-865", 863 * _MHZ, 865 * _MHZ),
    RangePreset("1785-1805", 1785 * _MHZ, 1805 * _MHZ),
    # EU ISM / SRD bands: 433.05-434.79 MHz and the 863-870 MHz SRD860 band.
    RangePreset("ISM 433", 433_050_000, 434_790_000),
    RangePreset("ISM 868", 863 * _MHZ, 870 * _MHZ),
    RangePreset("VHF 174-216", 174 * _MHZ, 216 * _MHZ),
)


def available(device_range_hz: tuple[int, int] | None) -> list[RangePreset]:
    """Presets the device can tune: those fully inside its range, then ``Overview``.

    Without a device (``None``) every fixed preset is offered and there is no ``Overview``.
    """
    if device_range_hz is None:
        return list(PRESETS)
    lo, hi = device_range_hz
    fitting = [p for p in PRESETS if lo <= p.start_hz and p.stop_hz <= hi]
    return [*fitting, RangePreset(OVERVIEW, lo, hi)]


def find(name: str, device_range_hz: tuple[int, int] | None) -> RangePreset | None:
    """The preset called ``name`` among :func:`available` ones, or ``None``."""
    return next((p for p in available(device_range_hz) if p.name == name), None)


__all__ = ["OVERVIEW", "PRESETS", "RangePreset", "available", "find"]
