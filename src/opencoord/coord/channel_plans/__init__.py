"""Channel plans: region data as TOML files shipped in this package.

``parse_plan`` is pure; ``load`` and ``available`` only read the package resources (read-only).
"""

from __future__ import annotations

from importlib import resources

from opencoord.coord.channel_plans.model import (
    BandAnnotation,
    Channel,
    ChannelPlan,
    Pmse,
    Span,
    parse_plan,
)

_SUFFIX = ".toml"


def available() -> list[str]:
    """Names of the packaged plans (file names without ``.toml``), sorted."""
    root = resources.files(__package__)
    return sorted(
        p.name[: -len(_SUFFIX)] for p in root.iterdir() if p.is_file() and p.name.endswith(_SUFFIX)
    )


def load(name: str) -> ChannelPlan:
    """The packaged plan ``name``; ``FileNotFoundError`` for an unknown name."""
    if name not in available():
        raise FileNotFoundError(f"no channel plan named {name!r}")
    text = (resources.files(__package__) / f"{name}{_SUFFIX}").read_text(encoding="utf-8")
    return parse_plan(text, name)


__all__ = [
    "BandAnnotation",
    "Channel",
    "ChannelPlan",
    "Pmse",
    "Span",
    "available",
    "load",
    "parse_plan",
]
