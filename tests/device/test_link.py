"""SerialLink against a fake serial port that replays recorded device bytes."""

from __future__ import annotations

import errno
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencoord.core.types import Sweep
from opencoord.device import protocol
from opencoord.device.link import (
    SerialLink,
    backoff_delays,
    classify_error,
    find_ports,
    user_message,
)
from opencoord.device.link_api import Link, LinkEvent

FIXTURES = Path(__file__).parent.parent / "fixtures"
F1 = (FIXTURES / "wsub1gplus_config_and_sweeps.bin").read_bytes()
# F1 ends in a sweep cut off by the end of the capture; replies use it without that tail.
F1_COMPLETE = F1[: F1.rfind(b"\r\n$S") + 2]
MHZ = 1_000_000
C0 = protocol.request_config()


# --- fake serial -----------------------------------------------------------------------------


Responder = Callable[[bytes], bytes]


class FakeSerial:
    """Records writes; ``responder(command)`` returns bytes the "device" sends back."""

    def __init__(self, responder: Responder | None = None, *, chunk: int = 97) -> None:
        self.responder = responder
        self.written: list[bytes] = []
        self.closed = False
        self.fail: BaseException | None = None
        self._chunk = chunk
        self._buf = bytearray()
        self._lock = threading.Lock()

    def inject(self, data: bytes) -> None:
        with self._lock:
            self._buf += data

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def write(self, data: bytes) -> int:
        if self.fail is not None:
            raise self.fail
        self.written.append(bytes(data))
        if self.responder is not None:
            self.inject(self.responder(bytes(data)))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self.fail is not None:
            raise self.fail
        with self._lock:
            n = min(size, self._chunk, len(self._buf))
            out = bytes(self._buf[:n])
            del self._buf[:n]
        if not out:
            time.sleep(0.002)  # a real port blocks for its read timeout
        return out

    def close(self) -> None:
        self.closed = True


def config_line(
    start_khz: int, step_hz: int, top: int = -10, bottom: int = -120, points: int = 112
) -> bytes:
    # Max span shrinks with points like the WSUB1G+ (959950 kHz at 112, 342370 kHz at 512).
    span_khz = 959_950 if points == 112 else 342_370 * 512 // points
    return b"#C2-F:%07d,%07d,%04d,%04d,%04d,0,000,0000050,0960000,%07d,00110,0000,004\r\n" % (
        start_khz,
        step_hz,
        top,
        bottom,
        points,
        span_khz,
    )


def sweep_frame(level: int = 200) -> bytes:
    return b"$S\x70" + bytes([level]) * 112 + b"\r\n"


_SET_CONFIG = re.compile(rb"C2-F:(\d{7}),(\d{7}),(-?\d{3,4}),(-?\d{3,4})")


def device(*, ignore_set_config: int = 0) -> Responder:
    """A responder that answers C0 with F1 and echoes set_config like the real device (F3)."""
    ignored = [ignore_set_config]
    points = [112]

    def respond(cmd: bytes) -> bytes:
        if cmd == C0:
            return F1_COMPLETE
        m = _SET_CONFIG.search(cmd)
        if m:
            if ignored[0] > 0:
                ignored[0] -= 1
                return b""
            start, stop = int(m[1]), int(m[2])
            step = round((stop - start) * 1000 / (points[0] - 1))
            line = config_line(start, step, int(m[3]), int(m[4]), points=points[0])
            return line + (sweep_frame() if points[0] == 112 else b"")
        if cmd.startswith(b"#\x05CJ"):  # set sweep points: the device keeps start and span
            points[0] = cmd[4] * 16 + 16
            return config_line(431_000, round(10_000_000 / (points[0] - 1)), points=points[0])
        return b""

    return respond


@dataclass
class Port:
    device: str
    vid: int | None
    pid: int | None
    description: str = ""


RFE_PORT = Port("/dev/ttyUSB0", 0x10C4, 0xEA60, "CP2102N USB to UART Bridge Controller")


class Factory:
    """Serial factory returning prepared fakes in order (the last one is reused)."""

    def __init__(self, *fakes: FakeSerial | BaseException) -> None:
        self.fakes = list(fakes)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, port: str, baud: int) -> FakeSerial:
        self.calls.append((port, baud))
        item = self.fakes.pop(0) if len(self.fakes) > 1 else self.fakes[0]
        if isinstance(item, BaseException):
            raise item
        return item


def make_link(factory: Factory, **kwargs: object) -> SerialLink:
    defaults: dict[str, object] = {
        "port_lister": lambda: [RFE_PORT],
        "platform": "linux",
        "reconnect_delay_s": 0.001,
        "reconnect_max_delay_s": 0.005,
        "command_timeout_s": 0.2,
    }
    defaults.update(kwargs)
    return SerialLink(serial_factory=factory, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def links() -> Iterator[list[SerialLink]]:
    created: list[SerialLink] = []
    yield created
    for link in created:
        link.close()


def wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def drain(q: queue.Queue[LinkEvent]) -> list[LinkEvent]:
    out: list[LinkEvent] = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def next_sweep(link: SerialLink, timeout: float = 3.0) -> Sweep:
    return link.sweeps.get(timeout=timeout)


def opened(links: list[SerialLink], fake: FakeSerial | None = None, **kwargs: object) -> SerialLink:
    fake = fake or FakeSerial(device())
    link = make_link(Factory(fake), **kwargs)
    links.append(link)
    link.open()
    return link


# --- port discovery --------------------------------------------------------------------------


def test_find_ports_lists_rf_explorer_first() -> None:
    other = Port("/dev/ttyACM0", 0x2341, 0x0043, "Arduino")
    plain = Port("/dev/ttyS0", None, None)
    ports = find_ports(lambda: [other, plain, RFE_PORT])
    assert [p.device for p in ports] == ["/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyS0"]
    assert [p.is_rf_explorer for p in ports] == [True, False, False]
    assert ports[0].description == "CP2102N USB to UART Bridge Controller"


# --- open / handshake ------------------------------------------------------------------------


def test_satisfies_link_protocol() -> None:
    link: Link = make_link(Factory(FakeSerial()))
    assert not link.is_open


def test_open_reads_model_and_config_then_streams_sweeps(links: list[SerialLink]) -> None:
    fake = FakeSerial(lambda cmd: F1 if cmd == C0 else b"")
    factory = Factory(fake)
    link = make_link(factory)
    links.append(link)
    link.open()
    assert link.is_open
    assert factory.calls == [("/dev/ttyUSB0", 500_000)]
    assert fake.written == [C0]
    assert link.model is not None and link.model.main_code == 10
    assert link.config is not None
    assert link.config.start_hz == 431_000_000 and link.config.sweep_points == 112
    assert link.capabilities is not None and link.capabilities.name == "RF Explorer WSUB1G+"
    events = drain(link.events)
    assert [e.kind for e in events] == ["connected"]
    assert "WSUB1G+" in events[0].message and "/dev/ttyUSB0" in events[0].message
    sweep = next_sweep(link)
    assert sweep.start_hz == 431_000_000 and sweep.dbm.shape == (112,)
    link.open()  # no-op when already open
    assert factory.calls == [("/dev/ttyUSB0", 500_000)]


def test_open_uses_the_given_port(links: list[SerialLink]) -> None:
    factory = Factory(FakeSerial(device()))
    link = make_link(factory, port="/dev/ttyUSB7", port_lister=lambda: [])
    links.append(link)
    link.open()
    assert factory.calls == [("/dev/ttyUSB7", 500_000)]


def test_open_falls_back_to_2400_baud(links: list[SerialLink]) -> None:
    silent = FakeSerial()
    factory = Factory(silent, FakeSerial(device()))
    link = make_link(factory)
    links.append(link)
    link.open(timeout_s=1.0)
    assert factory.calls == [("/dev/ttyUSB0", 500_000), ("/dev/ttyUSB0", 2_400)]
    assert silent.closed
    assert link.model is not None
    assert "2400 baud" in drain(link.events)[0].message


def test_open_resends_config_request_once_if_ignored() -> None:
    answers = [b"", F1]  # the device ignores the first C0 (busy), as seen on hardware
    fake = FakeSerial(lambda cmd: answers.pop(0) if cmd == C0 and answers else b"")
    link = make_link(Factory(fake))
    try:
        link.open(timeout_s=2.0)
        assert fake.written == [C0, C0]
    finally:
        link.close()


def test_open_without_reply_raises_and_emits_error() -> None:
    factory = Factory(FakeSerial())
    link = make_link(factory)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="No reply"):
        link.open(timeout_s=0.4)
    assert time.monotonic() - t0 < 1.0
    assert [b for _, b in factory.calls] == [500_000, 2_400]
    assert not link.is_open
    events = drain(link.events)
    assert [e.kind for e in events] == ["error"]
    assert "No reply" in events[0].message


def test_open_without_any_rf_explorer_port() -> None:
    link = make_link(Factory(FakeSerial()), port_lister=lambda: [Port("/dev/ttyS0", None, None)])
    with pytest.raises(ConnectionError, match="No RF Explorer found") as exc:
        link.open()
    assert "dmesg" in str(exc.value)
    assert drain(link.events)[0].kind == "error"


def test_open_maps_permission_error_to_group_help() -> None:
    denied = PermissionError(errno.EACCES, "Permission denied: '/dev/ttyUSB0'")
    link = make_link(Factory(denied))
    with pytest.raises(ConnectionError, match="dialout"):
        link.open()
    assert "uucp" in drain(link.events)[0].message


def test_open_tries_the_next_port_when_one_cannot_be_opened(links: list[SerialLink]) -> None:
    busy = OSError(errno.EBUSY, "Device or resource busy")
    second = Port("/dev/ttyUSB1", 0x10C4, 0xEA60)
    factory = Factory(busy, busy, FakeSerial(device()))
    link = make_link(factory, port_lister=lambda: [RFE_PORT, second])
    links.append(link)
    link.open()
    assert [c[0] for c in factory.calls] == ["/dev/ttyUSB0", "/dev/ttyUSB0", "/dev/ttyUSB1"]
    assert "/dev/ttyUSB1" in drain(link.events)[0].message


def test_open_reports_the_first_open_error_when_nothing_answers() -> None:
    busy = OSError(errno.EBUSY, "Device or resource busy")
    factory = Factory(busy, FakeSerial())
    link = make_link(factory)
    with pytest.raises(ConnectionError, match="in use"):
        link.open(timeout_s=0.2)
    assert [b for _, b in factory.calls] == [500_000, 2_400]


def test_open_maps_non_os_errors_from_the_factory() -> None:
    link = make_link(Factory(ValueError("Not a valid baudrate: 7")))
    with pytest.raises(ConnectionError, match="Not a valid baudrate"):
        link.open()
    assert drain(link.events)[0].kind == "error"


def test_no_reply_lists_tried_ports_and_bauds() -> None:
    second = Port("/dev/ttyUSB1", 0x10C4, 0xEA60)
    link = make_link(
        Factory(FakeSerial()), port_lister=lambda: [RFE_PORT, second], baud_rates=(500_000,)
    )
    with pytest.raises(ConnectionError) as exc:
        link.open(timeout_s=0.2)
    assert "/dev/ttyUSB0, /dev/ttyUSB1" in str(exc.value)
    assert "tried 500000 baud" in str(exc.value)


def test_open_maps_serial_exception_by_errno() -> None:
    serial = pytest.importorskip("serial")
    busy = serial.SerialException(errno.EAGAIN, "Could not exclusively lock port /dev/ttyUSB0")
    link = make_link(Factory(busy))
    with pytest.raises(ConnectionError, match="in use"):
        link.open()


# --- commands --------------------------------------------------------------------------------


def test_set_span_sends_set_config_and_updates_config_on_echo(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.set_span(470 * MHZ, 700 * MHZ)
    wait_for(lambda: link.config is not None and link.config.start_hz == 470 * MHZ)
    assert fake.written[1] == protocol.set_config(470 * MHZ, 700 * MHZ, -10, -120)
    assert link.config is not None and link.config.step_hz == 2_072_072
    wait_for(lambda: any_sweep_at(link, 470 * MHZ))


def any_sweep_at(link: SerialLink, start_hz: int) -> bool:
    try:
        return next_sweep(link, 0.05).start_hz == start_hz
    except queue.Empty:
        return False


def test_set_span_clamps_to_capabilities(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.set_span(0, 2_000 * MHZ)
    wait_for(lambda: len(fake.written) == 2)
    assert fake.written[1] == protocol.set_config(50_000, 960 * MHZ, -10, -120)


def test_set_span_keeps_at_least_one_khz(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.set_span(500_000_400, 500_000_600)
    wait_for(lambda: len(fake.written) == 2)
    m = _SET_CONFIG.search(fake.written[1])
    assert m is not None and int(m[2]) > int(m[1])


def test_set_span_rejects_bad_input_and_closed_link(links: list[SerialLink]) -> None:
    link = make_link(Factory(FakeSerial(device())))
    links.append(link)
    with pytest.raises(RuntimeError):
        link.set_span(470 * MHZ, 700 * MHZ)
    link.open()
    with pytest.raises(ValueError):
        link.set_span(700 * MHZ, 470 * MHZ)


def test_commands_wait_for_the_device_echo(links: list[SerialLink]) -> None:
    gate = threading.Event()
    base = device()

    def slow(cmd: bytes) -> bytes:
        reply = base(cmd)
        if cmd != C0:
            threading.Thread(target=lambda: (gate.wait(3), fake.inject(reply)), daemon=True).start()
            return b""
        return reply

    fake = FakeSerial(slow)
    link = opened(links, fake, command_timeout_s=2.0)
    link.set_span(470 * MHZ, 700 * MHZ)
    link.set_span(500 * MHZ, 600 * MHZ)
    wait_for(lambda: len(fake.written) == 2)
    time.sleep(0.1)
    assert len(fake.written) == 2  # second set_config held back until the first is confirmed
    gate.set()
    wait_for(lambda: len(fake.written) == 3)
    assert fake.written[2] == protocol.set_config(500 * MHZ, 600 * MHZ, -10, -120)


def test_unconfirmed_command_is_retried_once(links: list[SerialLink]) -> None:
    fake = FakeSerial(device(ignore_set_config=1))
    link = opened(links, fake)
    link.set_span(470 * MHZ, 700 * MHZ)
    wait_for(lambda: link.config is not None and link.config.start_hz == 470 * MHZ)
    assert fake.written[1] == fake.written[2]
    assert drain(link.events)[-1].kind == "connected"  # no error


def test_command_never_confirmed_reports_error_and_moves_on(links: list[SerialLink]) -> None:
    fake = FakeSerial(device(ignore_set_config=2))
    link = opened(links, fake, command_timeout_s=0.05)
    link.set_span(470 * MHZ, 700 * MHZ)
    link.set_span(500 * MHZ, 600 * MHZ)
    wait_for(lambda: link.config is not None and link.config.start_hz == 500 * MHZ)
    assert len(fake.written) == 4  # C0, set_config x2 (ignored), next set_config
    kinds = [e.kind for e in drain(link.events)]
    assert kinds == ["connected", "error"]


def test_long_hold_then_set_span_does_not_reconnect(links: list[SerialLink]) -> None:
    # Regression: silence while held must not count against the stall timeout after resuming.
    base = device()

    def delayed(cmd: bytes) -> bytes:
        reply = base(cmd)
        if cmd != C0 and reply:
            timer = threading.Timer(0.15, lambda: fake.inject(reply))  # ~hardware echo latency
            timer.daemon = True
            timer.start()
            return b""
        return reply

    fake = FakeSerial(delayed)
    factory = Factory(fake)
    link = make_link(factory, stall_timeout_s=0.3)
    links.append(link)
    link.open()
    link.hold()
    time.sleep(0.6)  # hold longer than the stall timeout
    link.set_span(470 * MHZ, 700 * MHZ)
    wait_for(lambda: link.config is not None and link.config.start_hz == 470 * MHZ)
    wait_for(lambda: any_sweep_at(link, 470 * MHZ))
    assert [e.kind for e in drain(link.events)] == ["connected"]
    assert len(factory.calls) == 1


def test_unconfirmed_set_config_is_resent_after_reconnect(links: list[SerialLink]) -> None:
    first = FakeSerial(device(ignore_set_config=1))
    second = FakeSerial(device())
    link = make_link(Factory(first, second), command_timeout_s=5.0)
    links.append(link)
    link.open()
    link.set_span(470 * MHZ, 700 * MHZ)
    request = protocol.set_config(470 * MHZ, 700 * MHZ, -10, -120)
    wait_for(lambda: request in first.written)
    first.fail = OSError(errno.EIO, "unplugged")
    wait_for(lambda: link.config is not None and link.config.start_hz == 470 * MHZ)
    assert second.written == [C0, request]
    assert [e.kind for e in drain(link.events)] == ["connected", "disconnected", "connected"]


def test_hold_then_set_span_resumes(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.hold()
    wait_for(lambda: protocol.hold() in fake.written)
    link.set_span(470 * MHZ, 700 * MHZ)
    wait_for(lambda: len(fake.written) == 3)
    assert fake.written[1:] == [
        protocol.hold(),
        protocol.set_config(470 * MHZ, 700 * MHZ, -10, -120),
    ]


def test_set_sweep_points_waits_for_the_config_echo(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.set_sweep_points(512)
    link.set_span(470 * MHZ, 700 * MHZ)
    wait_for(lambda: link.config is not None and link.config.start_hz == 470 * MHZ)
    assert fake.written[1:] == [
        protocol.set_sweep_points(512),
        protocol.set_config(470 * MHZ, 700 * MHZ, -10, -120),
    ]
    caps = link.capabilities
    assert caps is not None and caps.max_span_hz == 342_370_000  # re-resolved from the echo


def test_set_sweep_points_confirmed_and_validated(links: list[SerialLink]) -> None:
    link = make_link(Factory(FakeSerial(device())))
    links.append(link)
    with pytest.raises(RuntimeError):
        link.set_sweep_points(512)
    link.open()
    for bad in (100, 4097, 8192):
        with pytest.raises(ValueError):
            link.set_sweep_points(bad)
    link.set_sweep_points(512)
    wait_for(lambda: link.config is not None and link.config.sweep_points == 512)


def test_switch_module_without_expansion_reports_error(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    link.switch_module(True)
    wait_for(lambda: protocol.switch_module(True) in fake.written)
    link.switch_module(False)
    events = drain(link.events)
    assert events[-1].kind == "error" and "expansion" in events[-1].message
    assert protocol.switch_module(False) not in fake.written


def test_sweeps_with_wrong_point_count_are_dropped(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    time.sleep(0.1)
    while not link.sweeps.empty():
        link.sweeps.get_nowait()
    fake.inject(b"$S\x10" + b"\x80" * 16 + b"\r\n" + sweep_frame(150))
    sweep = next_sweep(link)
    assert sweep.dbm.shape == (112,) and float(sweep.dbm[0]) == -75.0


# --- close / reconnect -----------------------------------------------------------------------


def test_close_emits_disconnected_and_keeps_queues(links: list[SerialLink]) -> None:
    fake = FakeSerial(device())
    link = opened(links, fake)
    wait_for(lambda: not link.sweeps.empty())
    link.close()
    assert not link.is_open and fake.closed
    assert [e.kind for e in drain(link.events)] == ["connected", "disconnected"]
    assert not link.sweeps.empty()
    link.close()  # idempotent
    assert link.events.empty()


def test_reconnects_after_read_error(links: list[SerialLink]) -> None:
    first = FakeSerial(device())
    second = FakeSerial(device())
    factory = Factory(first, OSError(errno.ENOENT, "gone"), second)
    link = make_link(factory)
    links.append(link)
    link.open()
    first.fail = OSError(errno.EIO, "device reports readiness to read but returned no data")
    wait_for(lambda: second.written == [C0])
    wait_for(lambda: link.events.qsize() >= 3)
    kinds = [e.kind for e in drain(link.events)]
    assert kinds == ["connected", "disconnected", "connected"]
    assert first.closed and link.is_open
    while not link.sweeps.empty():
        link.sweeps.get_nowait()
    second.inject(sweep_frame(150))
    assert float(next_sweep(link).dbm[0]) == -75.0


def test_reconnects_when_the_device_goes_silent(links: list[SerialLink]) -> None:
    first = FakeSerial(device())
    second = FakeSerial(device())
    link = make_link(Factory(first, second), stall_timeout_s=0.2)
    links.append(link)
    link.open()
    wait_for(lambda: second.written == [C0])
    assert first.closed


def test_no_stall_reconnect_while_holding(links: list[SerialLink]) -> None:
    factory = Factory(FakeSerial(device()))
    link = make_link(factory, stall_timeout_s=0.1)
    links.append(link)
    link.open()
    link.hold()
    time.sleep(0.4)
    assert [e.kind for e in drain(link.events)] == ["connected"]
    assert len(factory.calls) == 1


def test_backoff_delays_double_up_to_max() -> None:
    delays = backoff_delays(0.5, 5.0)
    assert [next(delays) for _ in range(7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


# --- error messages (pure) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "platform", "kind"),
    [
        (PermissionError(errno.EACCES, "Permission denied"), "linux", "permission"),
        (OSError(errno.EACCES, "could not open port"), "darwin", "permission"),
        (PermissionError(13, "Access is denied."), "win32", "busy"),
        (
            OSError(
                None,
                "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)",
            ),
            "win32",
            "busy",
        ),
        (OSError(errno.EBUSY, "Device or resource busy"), "linux", "busy"),
        (OSError(errno.EWOULDBLOCK, "Could not exclusively lock port"), "linux", "busy"),
        (FileNotFoundError(errno.ENOENT, "No such file"), "linux", "not_found"),
        (
            OSError(
                None, "could not open port 'COM9': FileNotFoundError(2, 'The system cannot find')"
            ),
            "win32",
            "not_found",
        ),
        (OSError(errno.EIO, "I/O error"), "linux", "other"),
    ],
)
def test_classify_error(exc: BaseException, platform: str, kind: str) -> None:
    assert classify_error(exc, platform) == kind


def test_permission_help_is_os_specific() -> None:
    linux = user_message("permission", "/dev/ttyUSB0", "linux")
    assert "/dev/ttyUSB0" in linux and "dialout" in linux and "uucp" in linux
    assert "udev" in linux
    assert "dialout" not in user_message("permission", "/dev/cu.SLAB", "darwin")


def test_busy_help_names_rf_explorer_for_windows() -> None:
    assert "RF Explorer for Windows" in user_message("busy", "COM3", "win32")


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_not_found_help_links_cp210x_driver_on_windows_and_macos(platform: str) -> None:
    msg = user_message("not_found", None, platform)
    assert "No RF Explorer found" in msg
    assert "silabs.com" in msg and "CP210x" in msg


def test_not_found_help_on_linux_and_with_port() -> None:
    assert "silabs.com" not in user_message("not_found", None, "linux")
    assert "/dev/ttyUSB3" in user_message("not_found", "/dev/ttyUSB3", "linux")


def test_no_reply_and_other_messages() -> None:
    assert "No reply" in user_message("no_reply", "/dev/ttyUSB0", "linux")
    assert "tried 2400 baud" in user_message("no_reply", "COM3", "win32", bauds=(2400,))
    assert "boom" in user_message("other", "/dev/ttyUSB0", "linux", detail="boom")


def test_raw_sink_receives_every_byte_read() -> None:
    chunks: list[bytes] = []
    link = SerialLink(
        "/dev/ttyUSB0",
        serial_factory=Factory(FakeSerial(device())),
        port_lister=lambda: [RFE_PORT],
        raw_sink=chunks.append,
    )
    link.open()
    try:
        assert link.active_port == ("/dev/ttyUSB0", 500_000)
        deadline = time.monotonic() + 2
        while len(b"".join(chunks)) < len(F1_COMPLETE) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        link.close()
    assert b"".join(chunks).startswith(F1_COMPLETE)
