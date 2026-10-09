"""OpenCoord: RF Explorer spectrum scanner and frequency coordinator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("opencoord")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
