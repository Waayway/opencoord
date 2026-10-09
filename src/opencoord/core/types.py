"""Core value types shared by the device, core and UI layers.

Frequencies are ``int`` Hz (``float64`` Hz arrays for trace axes); levels are dBm floats.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, eq=False)
class Sweep:
    """One spectrum trace: a frequency axis and the level at each point.

    ``eq=False`` because numpy arrays have no single truth value; compare the arrays instead.
    """

    freqs_hz: npt.NDArray[np.float64]
    dbm: npt.NDArray[np.float32]
    timestamp: float

    @property
    def start_hz(self) -> int:
        return round(float(self.freqs_hz[0]))

    @property
    def stop_hz(self) -> int:
        return round(float(self.freqs_hz[-1]))


@dataclass(frozen=True)
class DeviceConfig:
    """Spectrum-analyzer configuration as reported by the device in ``#C2-F``.

    The device sends start/min/max/span/RBW in kHz and the step in Hz; all are stored as Hz here.
    ``rbw_hz``, ``amp_offset_db`` and ``calculator_mode`` are ``None`` for firmware that does not
    report them (before 1.09 / 1.12).
    """

    start_hz: int
    step_hz: int
    amp_top_dbm: float
    amp_bottom_dbm: float
    sweep_points: int
    expansion_active: bool
    mode: int
    min_hz: int
    max_hz: int
    max_span_hz: int
    rbw_hz: int | None
    amp_offset_db: float | None
    calculator_mode: int | None

    @property
    def stop_hz(self) -> int:
        """Frequency of the last sweep point."""
        return self.start_hz + (self.sweep_points - 1) * self.step_hz


@dataclass(frozen=True)
class ModelInfo:
    """Device model as reported in ``#C2-M``; ``expansion_code`` is ``None`` when absent (255)."""

    main_code: int
    expansion_code: int | None
    firmware: str


@dataclass(frozen=True, eq=False)
class Trace:
    """A labelled spectrum trace (live, max-hold, stitched scan, imported, ...).

    Same layout as :class:`Sweep` without the timestamp. ``eq=False`` because numpy arrays have no
    single truth value.
    """

    freqs_hz: npt.NDArray[np.float64]
    dbm: npt.NDArray[np.float32]
    label: str

    @property
    def start_hz(self) -> int:
        return round(float(self.freqs_hz[0]))

    @property
    def stop_hz(self) -> int:
        return round(float(self.freqs_hz[-1]))


@dataclass(frozen=True)
class Carrier:
    """A detected carrier: frequency in Hz and its level in dBm."""

    freq_hz: int
    level_dbm: float


@dataclass(frozen=True)
class ExclusionZone:
    """A frequency range the user wants kept clear (``start_hz < stop_hz``); ``id`` is 1-based."""

    id: int
    start_hz: int
    stop_hz: int
