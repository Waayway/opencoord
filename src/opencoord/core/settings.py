"""User settings persisted as ``settings.toml`` in the platform config directory.

Unknown keys are ignored and invalid values fall back to their defaults, so a hand-edited or
older file never stops the app from starting. TOML has no null: an unset ``last_port`` is
omitted and a custom range (``preset = None``) is written as ``preset = ""``.
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir

log = logging.getLogger(__name__)

APP_NAME = "opencoord"
_RESOLUTIONS = ("fast", "normal", "fine")
_MODES = ("live", "scan")
_MIN_WINDOW = 400
#: Waterfall history rows accepted by the settings, the controller and the scan panel.
WATERFALL_DEPTH_MIN = 10
WATERFALL_DEPTH_MAX = 1000


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
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if name in ("start_hz", "stop_hz"):
        return value >= 0
    if name in ("window_width", "window_height"):
        return value >= _MIN_WINDOW
    if name == "waterfall_depth":
        return WATERFALL_DEPTH_MIN <= value <= WATERFALL_DEPTH_MAX
    return False


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
    for field in dataclasses.fields(AppSettings):
        if field.name not in data:
            continue
        if _valid(field.name, data[field.name]):
            values[field.name] = data[field.name]
        else:
            log.warning("ignoring invalid setting %s = %r", field.name, data[field.name])
    if values.get("preset") == "":
        values["preset"] = None  # a custom range
    s = AppSettings(**values)
    if s.stop_hz <= s.start_hz:
        log.warning("ignoring invalid range in settings")
        s = dataclasses.replace(s, start_hz=AppSettings.start_hz, stop_hz=AppSettings.stop_hz)
    return s


def save(settings: AppSettings, path: Path | None = None) -> None:
    """Write ``settings`` to ``path`` (default :func:`default_path`), creating the directory."""
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(settings)
    data["preset"] = settings.preset or ""
    data = {k: v for k, v in data.items() if v is not None}
    text = "# OpenCoord settings (written by the app)\n" + tomli_w.dumps(data)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "WATERFALL_DEPTH_MAX",
    "WATERFALL_DEPTH_MIN",
    "AppSettings",
    "default_path",
    "load",
    "save",
]
