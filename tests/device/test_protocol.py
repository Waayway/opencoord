from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opencoord.core.types import DeviceConfig, ModelInfo
from opencoord.device import protocol
from opencoord.device.protocol import (
    ConfigReply,
    Event,
    ModelReply,
    ParseError,
    Parser,
    SweepData,
    Unknown,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
EEOT = b"\xff\xfe\xff\xfe\x00"

C2_F_112 = b"#C2-F:0431000,0090090,-010,-120,0112,0,000,0000050,0960000,0959950,00110,0000,004\r\n"
C2_M = b"#C2-M:010,255,03.39\r\n"


def _sweep_frame(samples: bytes, kind: bytes = b"S") -> bytes:
    if kind == b"S":
        header = b"$S" + bytes([len(samples)])
    elif kind == b"s":
        assert len(samples) % 16 == 0
        header = b"$s" + bytes([(len(samples) // 16) % 256])
    else:
        header = b"$z" + len(samples).to_bytes(2, "big")
    return header + samples + b"\r\n"


def _feed_all(data: bytes) -> list[Event]:
    return Parser().feed(data)


def _without_errors(events: list[Event]) -> list[Event]:
    return [e for e in events if not isinstance(e, ParseError)]


def _summary(events: list[Event]) -> list[object]:
    """Comparable view of events (SweepData holds numpy arrays)."""
    out: list[object] = []
    for e in events:
        if isinstance(e, SweepData):
            out.append(("sweep", e.samples.tolist()))
        else:
            out.append(e)
    return out


# --- command builders -------------------------------------------------------


def test_request_config() -> None:
    assert protocol.request_config() == b"#\x04C0"


def test_hold() -> None:
    assert protocol.hold() == b"#\x04CH"


def test_switch_module() -> None:
    assert protocol.switch_module(main=True) == b"#\x05CM\x00"
    assert protocol.switch_module(main=False) == b"#\x05CM\x01"


def test_set_config_matches_bytes_sent_to_real_device() -> None:
    # This exact command was sent while recording wsub1gplus_set_config_470_700.bin.
    cmd = protocol.set_config(470_000_000, 700_000_000, -20, -110)
    assert cmd == b"#\x20C2-F:0470000,0700000,-020,-110"
    assert len(cmd) == 32


def test_set_config_amplitude_formats() -> None:
    assert protocol.set_config(431_000_000, 441_000_000, 0, -120).endswith(b",0000,-120")
    assert protocol.set_config(431_000_000, 441_000_000, 5, -5).endswith(b",0005,-005")


@pytest.mark.parametrize(
    ("start", "stop", "top", "bottom"),
    [
        (500_000_000, 470_000_000, -10, -120),  # stop before start
        (-1, 470_000_000, -10, -120),
        (470_000_000, 10_000_000_000_000, -10, -120),  # > 7 kHz digits
        (470_000_000, 700_000_000, -120, -10),  # top below bottom
        (470_000_000, 700_000_000, 10_000, -120),
        (470_000_000, 700_000_000, -10, -1000),
    ],
)
def test_set_config_rejects_bad_values(start: int, stop: int, top: int, bottom: int) -> None:
    with pytest.raises(ValueError):
        protocol.set_config(start, stop, top, bottom)


def test_set_sweep_points_uses_cj_for_multiples_of_16_up_to_4096() -> None:
    # CJ 0x1f -> 512 points was verified on the device (wsub1gplus_512pt_z_sweeps.bin).
    assert protocol.set_sweep_points(512) == b"#\x05CJ\x1f"
    assert protocol.set_sweep_points(112) == b"#\x05CJ\x06"
    assert protocol.set_sweep_points(16) == b"#\x05CJ\x00"
    assert protocol.set_sweep_points(4096) == b"#\x05CJ\xff"


def test_set_sweep_points_uses_large_variant_otherwise() -> None:
    assert protocol.set_sweep_points(5000) == b"#\x06Cj\x13\x88"
    assert protocol.set_sweep_points(1000) == b"#\x06Cj\x03\xe8"


@pytest.mark.parametrize("n", [0, 15, 50, 65536, -16])
def test_set_sweep_points_rejects_out_of_range(n: int) -> None:
    with pytest.raises(ValueError):
        protocol.set_sweep_points(n)


# --- parser: synthetic frames ----------------------------------------------


def test_model_reply() -> None:
    events = _feed_all(C2_M)
    assert events == [ModelReply(ModelInfo(main_code=10, expansion_code=None, firmware="03.39"))]


def test_model_reply_with_expansion_and_spaces() -> None:
    (event,) = _feed_all(b"#C2-M:003, 005, 01.12\r\n")
    assert event == ModelReply(ModelInfo(main_code=3, expansion_code=5, firmware="01.12"))


def test_config_reply_converts_units_to_hz() -> None:
    (event,) = _feed_all(C2_F_112)
    assert isinstance(event, ConfigReply)
    assert event.config == DeviceConfig(
        start_hz=431_000_000,
        step_hz=90_090,
        amp_top_dbm=-10.0,
        amp_bottom_dbm=-120.0,
        sweep_points=112,
        expansion_active=False,
        mode=0,
        min_hz=50_000,
        max_hz=960_000_000,
        max_span_hz=959_950_000,
        rbw_hz=110_000,
        amp_offset_db=0.0,
        calculator_mode=4,
    )


def test_config_reply_old_firmware_without_optional_fields() -> None:
    (event,) = _feed_all(b"#C2-F:0431000,0090090,-010,-120,0112,1,000,0000050,0960000,0959950\r\n")
    assert isinstance(event, ConfigReply)
    assert event.config.expansion_active is True
    assert event.config.rbw_hz is None
    assert event.config.amp_offset_db is None
    assert event.config.calculator_mode is None


def test_config_reply_wide_step_and_five_digit_points() -> None:
    line = (
        b"#C2-F:0050000,10000000,-010,-120,12000,0,000,0000050,0960000,0959950,00600,-005,000\r\n"
    )
    (event,) = _feed_all(line)
    assert isinstance(event, ConfigReply)
    assert event.config.step_hz == 10_000_000
    assert event.config.sweep_points == 12_000
    assert event.config.amp_offset_db == -5.0


def test_malformed_config_is_parse_error() -> None:
    (event,) = _feed_all(b"#C2-F:04310x0,0090090\r\n")
    assert isinstance(event, ParseError)
    assert event.data.startswith(b"#C2-F:")


def test_other_hash_lines_are_unknown() -> None:
    events = _feed_all(b"#SnB36J37L7CB7KKKL3\r\n#a0\r\n")
    assert events == [Unknown("#SnB36J37L7CB7KKKL3"), Unknown("#a0")]


def test_plain_text_line_is_unknown() -> None:
    assert _feed_all(b"RF Explorer 03.39 21-Jun-22 05.01.08\r\n") == [
        Unknown("RF Explorer 03.39 21-Jun-22 05.01.08")
    ]


@pytest.mark.parametrize("kind", [b"S", b"s", b"z"])
def test_sweep_frames_decode_minus_half_byte(kind: bytes) -> None:
    raw = bytes(range(0, 256, 8)) * (2 if kind == b"s" else 1)
    (event,) = _feed_all(_sweep_frame(raw, kind))
    assert isinstance(event, SweepData)
    assert event.samples.dtype == np.float32
    np.testing.assert_array_equal(event.samples, np.frombuffer(raw, dtype=np.uint8) / -2.0)


def test_small_s_count_zero_means_4096_points() -> None:
    raw = bytes([0x80]) * 4096
    (event,) = _feed_all(b"$s\x00" + raw + b"\r\n")
    assert isinstance(event, SweepData)
    assert event.samples.shape == (4096,)


def test_z_count_is_big_endian() -> None:
    raw = bytes([200]) * 0x0102
    (event,) = _feed_all(b"$z\x01\x02" + raw + b"\r\n")
    assert isinstance(event, SweepData)
    assert event.samples.shape == (258,)
    assert event.samples[0] == -100.0


def test_sweep_may_contain_marker_and_crlf_bytes() -> None:
    raw = b"#$\r\n\x00\xff" * 10
    (event,) = _feed_all(_sweep_frame(raw))
    assert isinstance(event, SweepData)
    assert event.samples.shape == (60,)


def test_partial_frames_split_across_feeds() -> None:
    data = C2_M + C2_F_112 + _sweep_frame(bytes(range(112)))
    parser = Parser()
    events: list[Event] = []
    for i in range(len(data)):
        events.extend(parser.feed(data[i : i + 1]))
    assert _summary(events) == _summary(_feed_all(data))
    assert [type(e) for e in events] == [ModelReply, ConfigReply, SweepData]


def test_incomplete_frame_yields_nothing_yet() -> None:
    assert Parser().feed(b"$S\x10" + b"\x80" * 8) == []
    assert Parser().feed(b"$z\x02") == []
    assert Parser().feed(b"#C2-M:010,2") == []
    assert Parser().feed(b"RF Explorer 03.39\r") == []
    assert Parser().feed(EEOT[:3]) == []


def test_eeot_aborts_partial_sweep() -> None:
    # Seen on the device after a reconfiguration (wsub1gplus_512pt_z_sweeps.bin).
    data = b"$z\x02\x00" + EEOT + C2_M
    events = _feed_all(data)
    assert isinstance(events[0], ParseError)
    assert "EEOT" in events[0].reason
    assert events[1:] == [ModelReply(ModelInfo(10, None, "03.39"))]


def test_standalone_eeot_is_reported() -> None:
    events = _feed_all(EEOT + C2_M)
    assert isinstance(events[0], ParseError)
    assert "EEOT" in events[0].reason
    assert isinstance(events[1], ModelReply)


def test_garbage_resyncs_to_next_marker() -> None:
    data = b"\x8c\x87\x90\x00\x01" + C2_M + b"\x9e\xa5\xff" + C2_F_112
    events = _feed_all(data)
    assert [type(e) for e in events] == [ParseError, ModelReply, ParseError, ConfigReply]
    first = events[0]
    assert isinstance(first, ParseError)
    assert first.data == b"\x8c\x87\x90\x00\x01"


def test_sweep_with_wrong_terminator_resyncs() -> None:
    data = b"$S\x04\x80\x80\x80\x80XX" + C2_M
    events = _feed_all(data)
    assert isinstance(events[0], ParseError)
    assert _without_errors(events) == [ModelReply(ModelInfo(10, None, "03.39"))]


def test_unknown_dollar_frame_resyncs() -> None:
    events = _feed_all(b"$D\x01\x02" + C2_M)
    assert isinstance(events[0], ParseError)
    assert _without_errors(events) == [ModelReply(ModelInfo(10, None, "03.39"))]


def test_hash_line_with_binary_content_is_error() -> None:
    events = _feed_all(b"#\x80\x81\r\n" + C2_M)
    assert isinstance(events[0], ParseError)
    assert _without_errors(events) == [ModelReply(ModelInfo(10, None, "03.39"))]


def test_overlong_line_without_terminator_is_dropped() -> None:
    parser = Parser()
    events = parser.feed(b"#" + b"A" * 5000)
    assert len(events) == 1
    assert isinstance(events[0], ParseError)
    assert parser.feed(C2_M) == [ModelReply(ModelInfo(10, None, "03.39"))]


@settings(max_examples=300)
@given(st.binary(max_size=2000))
def test_parser_never_raises_on_random_bytes(data: bytes) -> None:
    parser = Parser()
    for i in range(0, len(data), 7):
        events = parser.feed(data[i : i + 7])
        assert all(
            isinstance(e, ModelReply | ConfigReply | SweepData | Unknown | ParseError)
            for e in events
        )


# --- parser: real device fixtures ------------------------------------------


def test_fixture_config_and_sweeps() -> None:
    data = (FIXTURES / "wsub1gplus_config_and_sweeps.bin").read_bytes()
    events = _feed_all(data)

    errors = [e for e in events if isinstance(e, ParseError)]
    assert len(errors) == 1 and "EEOT" in errors[0].reason  # leftover from the previous session

    models = [e for e in events if isinstance(e, ModelReply)]
    assert models == [ModelReply(ModelInfo(main_code=10, expansion_code=None, firmware="03.39"))]

    configs = [e.config for e in events if isinstance(e, ConfigReply)]
    assert len(configs) == 1
    config = configs[0]
    assert config.start_hz == 431_000_000
    assert config.step_hz == 90_090
    assert config.sweep_points == 112
    assert config.rbw_hz == 110_000
    assert config.min_hz == 50_000
    assert config.max_hz == 960_000_000
    assert config.calculator_mode == 4

    unknown = [e.line for e in events if isinstance(e, Unknown)]
    assert "#SnB36J37L7CB7KKKL3" in unknown
    assert unknown[0].startswith("RF Explorer 03.39")

    sweeps = [e for e in events if isinstance(e, SweepData)]
    assert len(sweeps) == 9  # the 10th is cut off by the end of the capture
    for sweep in sweeps:
        assert sweep.samples.shape == (112,)
        assert float(sweep.samples.min()) >= -128.0 and float(sweep.samples.max()) <= 0.0


def test_fixture_512_point_z_sweeps_with_eeot() -> None:
    data = (FIXTURES / "wsub1gplus_512pt_z_sweeps.bin").read_bytes()
    events = _feed_all(data)
    configs = [e.config for e in events if isinstance(e, ConfigReply)]
    assert [c.sweep_points for c in configs] == [512, 512]
    assert configs[0].step_hz == 19_569
    eeot = [e for e in events if isinstance(e, ParseError) and "EEOT" in e.reason]
    assert eeot  # the $z header immediately aborted after reconfiguration
    sweeps = [e for e in events if isinstance(e, SweepData)]
    assert len(sweeps) == 2
    assert all(s.samples.shape == (512,) for s in sweeps)


def test_fixture_set_config_reply() -> None:
    data = (FIXTURES / "wsub1gplus_set_config_470_700.bin").read_bytes()
    configs = [e.config for e in _feed_all(data) if isinstance(e, ConfigReply)]
    assert configs
    for config in configs:
        assert config.start_hz == 470_000_000
        assert config.step_hz == 2_072_072
        assert config.amp_top_dbm == -20.0
        assert config.amp_bottom_dbm == -110.0
        assert abs(config.stop_hz - 700_000_000) < config.step_hz


@pytest.mark.parametrize(
    "name",
    [
        "wsub1gplus_config_and_sweeps.bin",
        "wsub1gplus_512pt_z_sweeps.bin",
        "wsub1gplus_set_config_470_700.bin",
    ],
)
@settings(max_examples=50, deadline=None)
@given(cuts=st.lists(st.integers(min_value=0, max_value=4000), max_size=40))
def test_fixture_chunking_does_not_change_valid_events(name: str, cuts: list[int]) -> None:
    data = (FIXTURES / name).read_bytes()
    points = sorted({c % (len(data) + 1) for c in cuts})
    parser = Parser()
    events: list[Event] = []
    prev = 0
    for p in [*points, len(data)]:
        events.extend(parser.feed(data[prev:p]))
        prev = p
    assert _summary(_without_errors(events)) == _summary(_without_errors(_feed_all(data)))


# --- make_sweep --------------------------------------------------------------


def _config_112() -> DeviceConfig:
    (event,) = _feed_all(C2_F_112)
    assert isinstance(event, ConfigReply)
    return event.config


def test_make_sweep_builds_hz_axis() -> None:
    config = _config_112()
    samples = np.full(112, -100.0, dtype=np.float32)
    sweep = protocol.make_sweep(config, samples, timestamp=3.0)
    assert sweep.freqs_hz.dtype == np.float64
    assert sweep.dbm.dtype == np.float32
    assert sweep.start_hz == 431_000_000
    assert sweep.stop_hz == config.stop_hz == 431_000_000 + 111 * 90_090
    assert sweep.freqs_hz[1] - sweep.freqs_hz[0] == 90_090
    assert sweep.timestamp == 3.0
    np.testing.assert_array_equal(sweep.dbm, samples)


def test_make_sweep_applies_device_amplitude_offset() -> None:
    (event,) = _feed_all(
        b"#C2-F:0431000,0090090,-010,-120,0112,0,000,0000050,0960000,0959950,00110,-010,004\r\n"
    )
    assert isinstance(event, ConfigReply)
    sweep = protocol.make_sweep(event.config, np.full(112, -100.0, dtype=np.float32), 0.0)
    assert float(sweep.dbm[0]) == -110.0


def test_make_sweep_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        protocol.make_sweep(_config_112(), np.zeros(100, dtype=np.float32), 0.0)
