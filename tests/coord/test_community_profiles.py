"""Every community profile in ``profiles/`` must parse with the current parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencoord.coord.profiles import candidates, parse_profile
from opencoord.coord.spacing import builtin_presets

PROFILES = sorted((Path(__file__).resolve().parents[2] / "profiles").glob("*.toml"))


def test_there_is_an_example_profile() -> None:
    assert PROFILES


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.name)
def test_profile_parses(path: Path) -> None:
    profile = parse_profile(path.read_text(encoding="utf-8"), set(builtin_presets()))
    assert candidates(profile)
