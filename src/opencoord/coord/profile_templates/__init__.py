"""Built-in profile templates: TOML files shipped in this package (read-only data).

Parsing lives in ``opencoord.coord.profiles``; this package only hands out the text.
"""

from __future__ import annotations

from importlib import resources

_SUFFIX = ".toml"


def available() -> list[str]:
    """Names of the packaged templates (file names without ``.toml``), sorted."""
    root = resources.files(__package__)
    return sorted(
        p.name[: -len(_SUFFIX)] for p in root.iterdir() if p.is_file() and p.name.endswith(_SUFFIX)
    )


def read_text(name: str) -> str:
    """The TOML text of packaged template ``name``; ``FileNotFoundError`` if unknown."""
    if name not in available():
        raise FileNotFoundError(f"no built-in spacing template named {name!r}")
    return (resources.files(__package__) / f"{name}{_SUFFIX}").read_text(encoding="utf-8")
