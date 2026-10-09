"""Channel plan data model and the pure TOML parser (no I/O).

A plan file has a ``[plan]`` table (``title``), any number of ``[[channels]]`` rasters and
``[[bands]]`` annotations; frequencies in the file are MHz, in code Hz ``int``::

    [[channels]]            # channel N has centre  centre_base_mhz + width_mhz * N
    first = 21
    last = 48
    centre_base_mhz = 306
    width_mhz = 8
    pmse = "allowed"        # optional: allowed | forbidden | info

    [[bands]]
    start_mhz = 694
    stop_mhz = 790
    pmse = "forbidden"
    note = "700 MHz mobile band (not for PMSE in NL)"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Any, Literal, get_args

Pmse = Literal["allowed", "forbidden", "info"]
PMSE_VALUES: tuple[str, ...] = get_args(Pmse)
_MHZ = 1_000_000


@dataclass(frozen=True)
class Channel:
    """One channel: ``start_hz <= f < stop_hz``; ``pmse`` is ``None`` when the plan says nothing."""

    number: int
    start_hz: int
    stop_hz: int
    pmse: Pmse | None = None

    @property
    def centre_hz(self) -> int:
        return (self.start_hz + self.stop_hz) // 2


@dataclass(frozen=True)
class BandAnnotation:
    """A frequency range with a legality flag for PMSE use and a note for the user."""

    start_hz: int
    stop_hz: int
    pmse: Pmse
    note: str


@dataclass(frozen=True)
class Span:
    """A range to shade on the spectrum."""

    start_hz: int
    stop_hz: int
    pmse: Pmse
    note: str


@dataclass(frozen=True)
class ChannelPlan:
    name: str
    title: str
    channels: tuple[Channel, ...]
    bands: tuple[BandAnnotation, ...]

    def channel_at(self, freq_hz: float) -> Channel | None:
        """The channel containing ``freq_hz`` (lower edge inclusive), or ``None``."""
        for ch in self.channels:
            if ch.start_hz <= freq_hz < ch.stop_hz:
                return ch
        return None

    def shaded_spans(self) -> list[Span]:
        """Ranges to colour: runs of touching channels with the same flag, then the bands."""
        spans: list[Span] = []
        run: list[Channel] = []

        def flush() -> None:
            if run and run[0].pmse is not None:
                spans.append(Span(run[0].start_hz, run[-1].stop_hz, run[0].pmse, "Channels"))
            run.clear()

        for ch in sorted(self.channels, key=lambda c: c.start_hz):
            if run and (run[-1].stop_hz != ch.start_hz or run[-1].pmse != ch.pmse):
                flush()
            run.append(ch)
        flush()
        spans.extend(Span(b.start_hz, b.stop_hz, b.pmse, b.note) for b in self.bands)
        return spans


def _hz(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"{what} must be a non-negative number of MHz, got {value!r}")
    return round(value * _MHZ)


def _pmse(value: Any, what: str, *, required: bool) -> Pmse | None:
    if value is None and not required:
        return None
    if value not in PMSE_VALUES:
        raise ValueError(f"{what}: pmse must be one of {', '.join(PMSE_VALUES)}, got {value!r}")
    return value  # type: ignore[no-any-return]


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    return value


def parse_plan(text: str, name: str = "") -> ChannelPlan:
    """Parse plan TOML; raises ``ValueError`` (also for bad TOML) naming the problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"channel plan is not valid TOML: {exc}") from exc
    title = str(data.get("plan", {}).get("title", name))
    channels: list[Channel] = []
    for i, raster in enumerate(data.get("channels", [])):
        what = f"channels[{i}]"
        first, last = (
            _int(raster.get("first"), what + ".first"),
            _int(raster.get("last"), what + ".last"),
        )
        width = _hz(raster.get("width_mhz"), what + ".width_mhz")
        base = _hz(raster.get("centre_base_mhz"), what + ".centre_base_mhz")
        if last < first or width <= 0:
            raise ValueError(f"{what}: needs first <= last and a positive width_mhz")
        pmse = _pmse(raster.get("pmse"), what, required=False)
        for n in range(first, last + 1):
            centre = base + width * n
            channels.append(Channel(n, centre - width // 2, centre + width // 2, pmse))
    bands: list[BandAnnotation] = []
    for i, band in enumerate(data.get("bands", [])):
        what = f"bands[{i}]"
        start, stop = _hz(band.get("start_mhz"), what), _hz(band.get("stop_mhz"), what)
        if stop <= start:
            raise ValueError(f"{what}: stop_mhz must be greater than start_mhz")
        flag = _pmse(band.get("pmse"), what, required=True)
        assert flag is not None
        bands.append(BandAnnotation(start, stop, flag, str(band.get("note", ""))))
    return ChannelPlan(name, title, tuple(channels), tuple(bands))
