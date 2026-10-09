"""User settings persisted as ``settings.toml`` in the platform config directory.

Unknown keys are ignored and invalid values fall back to their defaults, so a hand-edited or
older file never stops the app from starting. TOML has no null: an unset ``last_port`` is
omitted and a custom range (``preset = None``) is written as ``preset = ""``.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir

from opencoord.io.atomic import write_atomic

log = logging.getLogger(__name__)

APP_NAME = "opencoord"
_RESOLUTIONS = ("fast", "normal", "fine")
_MODES = ("live", "scan")
_MIN_WINDOW = 400
#: Waterfall history rows accepted by the settings, the controller and the scan panel.
WATERFALL_DEPTH_MIN = 10
WATERFALL_DEPTH_MAX = 1000
#: Largest amplitude offset (dB, either sign) accepted by the settings and the controller.
AMP_OFFSET_LIMIT_DB = 50.0


@dataclass(frozen=True)
class AppSettings:
    last_port: str | None = None
    auto_connect: bool = False
    preset: str | None = "Full UHF 470-960"
    resolution: str = "normal"
    start_hz: int = 470_000_000
    stop_hz: int = 960_000_000
    window_width: int = 1280
    window_height: int = 800
    waterfall_depth: int = 300
    mode: str = "live"
    #: Threshold line level; ``None`` = hidden (omitted from the file, TOML has no null).
    threshold_dbm: float | None = None
    #: Amplitude offset in dB by device key (``model_<code>``, see the controller); 0 is not stored.
    amp_offsets: dict[str, float] = field(default_factory=dict)


def default_path() -> Path:
    return Path(user_config_dir(APP_NAME)) / "settings.toml"


def _valid(name: str, value: Any) -> bool:
    if name == "last_port":
        return isinstance(value, str) and value != ""
    if name == "preset":
        return isinstance(value, str)
    if name == "auto_connect":
        return isinstance(value, bool)
    if name == "resolution":
        return value in _RESOLUTIONS
    if name == "mode":
        return value in _MODES
    if name == "threshold_dbm":
        return (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and -200 <= value <= 50
        )
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if name in ("start_hz", "stop_hz"):
        return value >= 0
    if name in ("window_width", "window_height"):
        return value >= _MIN_WINDOW
    if name == "waterfall_depth":
        return WATERFALL_DEPTH_MIN <= value <= WATERFALL_DEPTH_MAX
    return False


def _amp_offsets(value: Any) -> dict[str, float]:
    """The valid entries of an ``[amp_offsets]`` table (others are logged and dropped)."""
    if not isinstance(value, dict):
        log.warning("ignoring invalid setting amp_offsets = %r", value)
        return {}
    out: dict[str, float] = {}
    for key, offset in value.items():
        ok = (
            isinstance(offset, int | float)
            and not isinstance(offset, bool)
            and math.isfinite(offset)
            and abs(offset) <= AMP_OFFSET_LIMIT_DB
        )
        if ok:
            out[str(key)] = float(offset)
        else:
            log.warning("ignoring invalid amp_offsets entry %s = %r", key, offset)
    return out


def load(path: Path | None = None) -> AppSettings:
    """Settings from ``path`` (default :func:`default_path`); defaults if missing or unreadable."""
    path = path or default_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return AppSettings()
    except (OSError, ValueError) as exc:  # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        log.warning("ignoring unreadable settings file %s: %s", path, exc)
        return AppSettings()
    values: dict[str, Any] = {}
    for fld in dataclasses.fields(AppSettings):
        if fld.name not in data:
            continue
        if fld.name == "amp_offsets":
            values["amp_offsets"] = _amp_offsets(data["amp_offsets"])
        elif _valid(fld.name, data[fld.name]):
            values[fld.name] = data[fld.name]
        else:
            log.warning("ignoring invalid setting %s = %r", fld.name, data[fld.name])
    if values.get("preset") == "":
        values["preset"] = None  # a custom range
    if "threshold_dbm" in values:
        values["threshold_dbm"] = float(values["threshold_dbm"])
    s = AppSettings(**values)
    if s.stop_hz <= s.start_hz:
        log.warning("ignoring invalid range in settings")
        s = dataclasses.replace(s, start_hz=AppSettings.start_hz, stop_hz=AppSettings.stop_hz)
    return s


def save(settings: AppSettings, path: Path | None = None) -> None:
    """Write ``settings`` to ``path`` (default :func:`default_path`), creating the directory."""
    path = path or default_path()
    data = dataclasses.asdict(settings)
    data["preset"] = settings.preset or ""
    data = {k: v for k, v in data.items() if v is not None and v != {}}
    text = "# OpenCoord settings (written by the app)\n" + tomli_w.dumps(data)
    write_atomic(path, text.encode("utf-8"))  # creates the directory too


__all__ = [
    "AMP_OFFSET_LIMIT_DB",
    "WATERFALL_DEPTH_MAX",
    "WATERFALL_DEPTH_MIN",
    "AppSettings",
    "default_path",
    "load",
    "save",
]
