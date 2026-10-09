"""Device profiles: parsing, validation, serialising, candidates, templates."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opencoord.coord.profiles import (
    ChannelGroup,
    DeviceProfile,
    ProfileError,
    TuningRange,
    builtin_templates,
    candidates,
    parse_profile,
    profile_to_toml,
)
from opencoord.coord.spacing import PartialSpacing

MHZ = 1_000_000
PRESETS = ["generic-analog", "iem"]

TUNED = """
[profile]
name = "Handheld"
kind = "mic"
tuning = [[823.0, 832.0]]
step_khz = 25
spacing = "generic-analog"

[spacing]
im3_2tx = 120
im5_3tx = 0
"""


def test_parse_tuning_profile() -> None:
    p = parse_profile(TUNED, PRESETS)
    assert p.name == "Handheld"
    assert p.kind == "mic"
    assert p.tuning == (TuningRange(823 * MHZ, 832 * MHZ),)
    assert p.step_hz == 25_000
    assert p.spacing_preset == "generic-analog"
    assert p.spacing_overrides == PartialSpacing(im3_2tx=120_000, im5_3tx=0)


def test_candidates_from_tuning_include_both_ends_and_are_sorted() -> None:
    p = parse_profile(TUNED, PRESETS)
    c = candidates(p)
    assert c[0].freq_hz == 823 * MHZ
    assert c[-1].freq_hz == 832 * MHZ
    assert len(c) == 9 * 40 + 1
    assert [x.freq_hz for x in c] == sorted({x.freq_hz for x in c})
    assert all(x.groups == () for x in c)


def test_candidates_from_overlapping_ranges_are_deduplicated() -> None:
    p = DeviceProfile(
        "x",
        "mic",
        "iem",
        tuning=(TuningRange(100 * MHZ, 100_100_000), TuningRange(100_050_000, 100_200_000)),
        step_hz=50_000,
    )
    assert [x.freq_hz for x in candidates(p)] == [
        100_000_000,
        100_050_000,
        100_100_000,
        100_150_000,
        100_200_000,
    ]


def test_candidates_from_channels() -> None:
    p = parse_profile(
        '[profile]\nname="c"\nkind="iem"\nchannels=[864.5, 863.125]\nspacing="iem"\n', PRESETS
    )
    assert [x.freq_hz for x in candidates(p)] == [863_125_000, 864_500_000]


def test_candidates_from_groups_expose_membership() -> None:
    text = (
        '[profile]\nname="g"\nkind="mic"\nspacing="iem"\n'
        '[[groups]]\nname="A"\nchannels=[863.0, 864.0]\n'
        '[[groups]]\nname="B"\nchannels=[864.0, 865.0]\n'
    )
    p = parse_profile(text, PRESETS)
    assert p.groups == (
        ChannelGroup("A", (863 * MHZ, 864 * MHZ)),
        ChannelGroup("B", (864 * MHZ, 865 * MHZ)),
    )
    got = [(c.freq_hz, c.groups) for c in candidates(p)]
    assert got == [(863 * MHZ, ("A",)), (864 * MHZ, ("A", "B")), (865 * MHZ, ("B",))]


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("junk [", "TOML"),
        ("", "[profile]"),
        ('[profile]\nkind="mic"\nchannels=[1.0]\nspacing="iem"\n', "name"),
        ('[profile]\nname="a"\nkind="radio"\nchannels=[1.0]\nspacing="iem"\n', "kind.*radio"),
        ('[profile]\nname="a"\nkind="mic"\nchannels=[1.0]\nspacing="nope"\n', "nope.*iem"),
        ('[profile]\nname="a"\nkind="mic"\nchannels=[1.0]\n', "spacing"),
        ('[profile]\nname="a"\nkind="mic"\nspacing="iem"\n', "tuning.*channels.*groups"),
        (
            '[profile]\nname="a"\nkind="mic"\nspacing="iem"\ntuning=[[1.0,2.0]]\nchannels=[1.0]\n',
            "only one",
        ),
        ('[profile]\nname="a"\nkind="mic"\nspacing="iem"\ntuning=[[1.0,2.0]]\n', "step_khz"),
        (
            '[profile]\nname="a"\nkind="mic"\nspacing="iem"\ntuning=[[1.0,2.0]]\nstep_khz=0\n',
            "step_khz",
        ),
        (
            '[profile]\nname="a"\nkind="mic"\nspacing="iem"\ntuning=[[5.0,2.0]]\nstep_khz=25\n',
            r"tuning\[0\]",
        ),
        ('[profile]\nname="a"\nkind="mic"\nspacing="iem"\nchannels=[1.0, 1.0]\n', "duplicate"),
        ('[profile]\nname="a"\nkind="mic"\nspacing="iem"\nchannels=[-1.0]\n', "channels"),
        ('[profile]\nname="a"\nkind="mic"\nspacing="iem"\nchannels=["x"]\n', "channels"),
        (
            '[profile]\nname="a"\nkind="mic"\nspacing="iem"\n'
            '[[groups]]\nname="A"\nchannels=[1.0]\n[[groups]]\nname="A"\nchannels=[2.0]\n',
            "group.*A",
        ),
        (
            '[profile]\nname="a"\nkind="mic"\nspacing="iem"\n[[groups]]\nname="A"\nchannels=[]\n',
            "group.*A",
        ),
        (TUNED + "im9 = 3\n", "im9"),
        (TUNED.replace('kind = "mic"', 'kind = "mic"\nstepp = 3'), "stepp"),
        (TUNED + "[extra]\na = 1\n", "extra"),
        (TUNED.replace("823.0, 832.0", "823.0, 1e300"), "tuning"),
        (TUNED.replace("step_khz = 25", "step_khz = 1e300"), "step_khz"),
        (TUNED.replace("im3_2tx = 120", "im3_2tx = 1e300"), "im3_2tx"),
        (TUNED.replace("im5_3tx = 0", "im5_3tx = -1"), "im5_3tx"),
    ],
)
def test_validation_errors_are_readable(text: str, needle: str) -> None:
    with pytest.raises(ProfileError, match=needle):
        parse_profile(text, PRESETS)


def test_huge_candidate_count_is_rejected() -> None:
    text = (
        '[profile]\nname="a"\nkind="mic"\nspacing="iem"\ntuning=[[100.0, 1000.0]]\nstep_khz=0.001\n'
    )
    with pytest.raises(ProfileError, match="candidates"):
        parse_profile(text, PRESETS)


def test_builtin_templates() -> None:
    t = builtin_templates()
    assert sorted(p.name for p in t.values()) == [
        "Fixed-channel set",
        "Generic IEM",
        "Generic analog mic",
        "Generic digital",
    ]
    assert t["generic-iem"].kind == "iem"
    assert t["fixed-channel-set"].groups
    for p in t.values():
        assert candidates(p)


freq = st.integers(min_value=1_000_000, max_value=6_000_000_000)
opt_hz = st.one_of(st.none(), st.integers(min_value=0, max_value=2_000_000_000))
partial_st = st.builds(PartialSpacing, opt_hz, opt_hz, opt_hz, opt_hz, opt_hz, opt_hz)
names = st.text(alphabet=st.characters(categories=("L", "N")), min_size=1, max_size=12)


@st.composite
def profiles(draw: st.DrawFn) -> DeviceProfile:
    source = draw(st.sampled_from(["tuning", "channels", "groups"]))
    common = {
        "name": draw(names),
        "kind": draw(st.sampled_from(["mic", "iem", "other"])),
        "spacing_preset": draw(st.sampled_from(PRESETS)),
        "spacing_overrides": draw(partial_st),
    }
    if source == "tuning":
        ranges = []
        for _ in range(draw(st.integers(1, 3))):
            a = draw(freq)
            ranges.append(TuningRange(a, a + draw(st.integers(1, 20_000_000))))
        step = draw(st.integers(min_value=1000, max_value=1_000_000)) // 1000 * 1000
        return DeviceProfile(**common, tuning=tuple(ranges), step_hz=step)  # type: ignore[arg-type]
    if source == "channels":
        ch = tuple(draw(st.lists(freq, min_size=1, max_size=8, unique=True)))
        return DeviceProfile(**common, channels=ch)  # type: ignore[arg-type]
    gnames = draw(st.lists(names, min_size=1, max_size=3, unique=True))
    groups = tuple(
        ChannelGroup(n, tuple(draw(st.lists(freq, min_size=1, max_size=5, unique=True))))
        for n in gnames
    )
    return DeviceProfile(**common, groups=groups)  # type: ignore[arg-type]


@given(profiles())
def test_serialise_parse_round_trip(p: DeviceProfile) -> None:
    assert parse_profile(profile_to_toml(p), PRESETS) == p
