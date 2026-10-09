"""Serial connection to a real RF Explorer: discovery, handshake, streaming, reconnect.

``SerialLink`` implements the ``Link`` contract (``device/link_api.py``). After ``open()`` a single
worker thread owns the port: it writes queued commands one at a time (waiting for the device's
``#C2-F`` confirmation where there is one, because the device may ignore a command sent while it
is still busy), feeds everything it reads into :class:`protocol.Parser`, and reconnects with
backoff when the port fails or goes silent.

The serial factory and the port lister are injectable so tests can use a fake port that replays
recorded device bytes.
"""

from __future__ import annotations

import errno
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep
from opencoord.device import models, protocol
from opencoord.device.link_api import LinkEvent, check_sweep_points, put_drop_oldest
from opencoord.device.models import Capabilities

log = logging.getLogger(__name__)

#: USB VID:PID of the Silicon Labs CP210x bridge inside RF Explorer units.
RF_EXPLORER_USB_ID = (0x10C4, 0xEA60)
#: Baud rates tried in order (500000 verified on the WSUB1G+; 2400 is the device's other setting).
BAUD_RATES = (500_000, 2_400)
CP210X_DRIVER_URL = "https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"
UDEV_RULE = "99-opencoord-rfexplorer.rules"

_READ_TIMEOUT_S = 0.05
_QUEUE_SIZE = 64
_MIN_SPAN_HZ = 1000  # set_config works in whole kHz


# --- injectable I/O ------------------------------------------------------------------------------


class SerialLike(Protocol):
    """The part of ``serial.Serial`` the link uses."""

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes, /) -> int | None: ...

    def close(self) -> None: ...


class PortInfoLike(Protocol):
    """The part of ``serial.tools.list_ports_common.ListPortInfo`` the link uses."""

    @property
    def device(self) -> str: ...

    @property
    def vid(self) -> int | None: ...

    @property
    def pid(self) -> int | None: ...

    @property
    def description(self) -> str: ...


SerialFactory = Callable[[str, int], SerialLike]
PortLister = Callable[[], Iterable[PortInfoLike]]


def open_serial(port: str, baud: int) -> SerialLike:
    """Default factory: 8N1 with a short read timeout, locked against other programs on POSIX."""
    import serial  # imported lazily so the module loads without pyserial in pure tests

    return cast(
        SerialLike,
        serial.Serial(
            port=port, baudrate=baud, timeout=_READ_TIMEOUT_S, write_timeout=1.0, exclusive=True
        ),
    )


def list_serial_ports() -> list[PortInfoLike]:
    from serial.tools import list_ports

    return cast(list[PortInfoLike], list(list_ports.comports()))


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str
    is_rf_explorer: bool


def find_ports(lister: PortLister = list_serial_ports) -> list[SerialPort]:
    """All serial ports, RF Explorer candidates (VID:PID ``10c4:ea60``) first."""
    ports = [
        SerialPort(p.device, p.description or "", (p.vid, p.pid) == RF_EXPLORER_USB_ID)
        for p in lister()
    ]
    return sorted(ports, key=lambda p: not p.is_rf_explorer)


# --- user-facing errors (pure) -------------------------------------------------------------------

ErrorKind = Literal["permission", "busy", "not_found", "no_reply", "other"]


def classify_error(exc: BaseException, platform: str) -> ErrorKind:
    """Sort an exception from opening/using a port into a kind with its own help text.

    ``serial.SerialException`` is an ``OSError``; on POSIX it carries the errno, on Windows only
    the text of the wrapped ``WinError`` (where "Access is denied" means another program has the
    port open).
    """
    code = getattr(exc, "errno", None)
    text = str(exc).lower()
    if isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        return "busy" if platform == "win32" else "permission"
    if "permissionerror" in text or "access is denied" in text:
        return "busy" if platform == "win32" else "permission"
    if code in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK) or "lock" in text or "busy" in text:
        return "busy"
    if (
        isinstance(exc, FileNotFoundError)
        or code in (errno.ENOENT, errno.ENODEV, errno.ENXIO)
        or "filenotfounderror" in text
        or "cannot find" in text
    ):
        return "not_found"
    return "other"


def user_message(
    kind: ErrorKind,
    port: str | None,
    platform: str,
    detail: str = "",
    bauds: Sequence[int] = BAUD_RATES,
) -> str:
    """The message shown to the user for ``kind``, with help for ``platform`` (``sys.platform``).

    ``port`` may name several ports (``"/dev/ttyUSB0, /dev/ttyUSB1"``); ``bauds`` are the rates
    tried, for ``no_reply``.
    """
    where = port or "the serial port"
    if kind == "permission":
        if platform.startswith("linux"):
            return (
                f"Permission denied opening {where}. Add yourself to the serial port group and "
                "log out and in again: 'sudo usermod -aG dialout $USER' (Debian, Ubuntu, Fedora) "
                f"or 'sudo usermod -aG uucp $USER' (Arch), or install the udev rule {UDEV_RULE}."
            )
        return (
            f"Permission denied opening {where}. Check that your user may use serial ports "
            "and that no other program has it open."
        )
    if kind == "busy":
        return (
            f"{where} is in use by another program. Close RF Explorer for Windows, Touchstone, "
            "Wireless Workbench or any other serial tool, then try again."
        )
    if kind == "not_found":
        head = f"{port} not found." if port else "No RF Explorer found."
        if platform in ("win32", "darwin"):
            return (
                f"{head} Connect it with a USB data cable and switch it on. If it still does not "
                f"show up, install the Silicon Labs CP210x USB driver: {CP210X_DRIVER_URL}"
            )
        return (
            f"{head} Connect it with a USB data cable and switch it on; 'dmesg' should show a "
            "cp210x converter attached to ttyUSB."
        )
    if kind == "no_reply":
        tried = " and ".join(str(b) for b in bauds)
        return (
            f"No reply from an RF Explorer on {where} (tried {tried} baud). Switch it on, leave "
            "any menu on its screen and try again."
        )
    return f"Could not use {where}: {detail}" if detail else f"Could not use {where}."


def backoff_delays(initial_s: float, max_s: float) -> Iterator[float]:
    """Reconnect delays: ``initial_s`` doubling up to ``max_s``, then ``max_s`` forever."""
    delay = initial_s
    while True:
        yield delay
        delay = min(delay * 2, max_s)


# --- the link ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Command:
    kind: Literal["set_config", "set_sweep_points", "hold", "switch_module"]
    data: bytes

    @property
    def confirmed_by_config(self) -> bool:
        """The device answers with ``#C2-F``, so the next command waits for it."""
        return self.kind in ("set_config", "set_sweep_points")


@dataclass
class _Connection:
    serial: SerialLike
    port: str
    baud: int
    parser: protocol.Parser
    model: ModelInfo
    config: DeviceConfig
    backlog: list[protocol.Event] = field(default_factory=list)  # read after the handshake


class _LinkLost(Exception):
    pass


class SerialLink:
    """``Link`` implementation for an RF Explorer on a serial port."""

    def __init__(
        self,
        port: str | None = None,
        *,
        serial_factory: SerialFactory = open_serial,
        port_lister: PortLister = list_serial_ports,
        baud_rates: Sequence[int] = BAUD_RATES,
        queue_size: int = _QUEUE_SIZE,
        command_timeout_s: float = 1.0,
        reconnect_delay_s: float = 0.5,
        reconnect_max_delay_s: float = 5.0,
        stall_timeout_s: float = 10.0,
        platform: str = sys.platform,
        raw_sink: Callable[[bytes], None] | None = None,
    ) -> None:
        self._port = port
        self._raw_sink = raw_sink  # called from the reader thread with every chunk read
        self._active: tuple[str, int] | None = None
        self._factory = serial_factory
        self._lister = port_lister
        self._bauds = tuple(baud_rates)
        self._command_timeout = command_timeout_s
        self._backoff = (reconnect_delay_s, reconnect_max_delay_s)
        self._stall_timeout = stall_timeout_s
        self._platform = platform
        self.sweeps: queue.Queue[Sweep] = queue.Queue(maxsize=queue_size)
        self.events: queue.Queue[LinkEvent] = queue.Queue(maxsize=queue_size)
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._model: ModelInfo | None = None
        self._config: DeviceConfig | None = None
        self._capabilities: Capabilities | None = None
        self._connect_timeout = 5.0
        # Command written but not yet confirmed; survives a reconnect (worker thread only).
        self._in_flight: _Command | None = None

    # --- Link properties ---

    @property
    def active_port(self) -> tuple[str, int] | None:
        """``(device, baud)`` of the current connection, ``None`` before the first one."""
        return self._active

    @property
    def model(self) -> ModelInfo | None:
        return self._model

    @property
    def config(self) -> DeviceConfig | None:
        return self._config

    @property
    def capabilities(self) -> Capabilities | None:
        return self._capabilities

    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def retunable(self) -> bool:
        return True

    # --- Link methods ---

    def open(self, timeout_s: float = 5.0) -> None:
        if self.is_open:
            return
        self._stop.clear()
        self._connect_timeout = timeout_s
        while not self._commands.empty():
            self._commands.get_nowait()
        self._in_flight = None
        try:
            conn = self._connect(timeout_s)
        except ConnectionError as exc:
            put_drop_oldest(self.events, LinkEvent("error", str(exc)))
            raise
        self._adopt(conn)
        put_drop_oldest(self.events, LinkEvent("connected", self._connected_message(conn)))
        self._thread = threading.Thread(
            target=self._run, args=(conn,), name="serial-link", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=3.0)
        if thread.is_alive():  # still running: stay "open" rather than lie
            log.warning("serial link thread did not stop within 3 s; link stays open")
            return
        self._thread = None
        put_drop_oldest(self.events, LinkEvent("disconnected", "RF Explorer closed"))

    def set_span(self, start_hz: int, stop_hz: int) -> None:
        if stop_hz <= start_hz:
            raise ValueError("stop must be greater than start")
        with self._lock:
            caps, config = self._capabilities, self._config
        if not self.is_open or caps is None or config is None:
            raise RuntimeError("RF Explorer is not open")
        min_span = max(config.sweep_points - 1, _MIN_SPAN_HZ)
        start = max(start_hz, caps.min_hz)
        stop = min(stop_hz, caps.max_hz)
        if stop - start > caps.max_span_hz:
            stop = start + caps.max_span_hz
        if stop - start < min_span:
            stop = min(start + min_span, caps.max_hz)
            start = stop - min_span
        data = protocol.set_config(
            start, stop, round(config.amp_top_dbm), round(config.amp_bottom_dbm)
        )
        self._commands.put(_Command("set_config", data))

    def set_sweep_points(self, points: int) -> None:
        with self._lock:
            caps = self._capabilities
        if not self.is_open or caps is None:
            raise RuntimeError("RF Explorer is not open")
        check_sweep_points(points, caps)
        self._commands.put(_Command("set_sweep_points", protocol.set_sweep_points(points)))

    def hold(self) -> None:
        if self.is_open:
            self._commands.put(_Command("hold", protocol.hold()))

    def switch_module(self, main: bool) -> None:
        model = self._model
        if not main and model is not None and model.expansion_code is None:
            put_drop_oldest(self.events, LinkEvent("error", "This device has no expansion module"))
            return
        if self.is_open:
            # ⚠ The reply to CM is not verified; a new #C2-F is applied whenever it arrives.
            self._commands.put(_Command("switch_module", protocol.switch_module(main)))

    # --- connecting ---

    def _connect(self, timeout_s: float) -> _Connection:
        """Find the device and handshake; raises ``ConnectionError`` with a user message."""
        if self._port is not None:
            ports = [self._port]
        else:
            ports = [p.device for p in find_ports(self._lister) if p.is_rf_explorer]
            if not ports:
                raise ConnectionError(user_message("not_found", None, self._platform))
        deadline = time.monotonic() + timeout_s
        attempts = [(port, baud) for port in ports for baud in self._bauds]
        first_error: tuple[Exception, str] | None = None  # reported if nothing answers
        for i, (port, baud) in enumerate(attempts):
            if self._stop.is_set():
                break
            budget = (deadline - time.monotonic()) / (len(attempts) - i)
            try:
                ser = self._factory(port, baud)
            except Exception as exc:  # pyserial raises ValueError too, e.g. for a bad baud rate
                log.debug("cannot open %s at %d baud: %s", port, baud, exc)
                first_error = first_error or (exc, port)
                continue
            try:
                conn = self._handshake(ser, port, baud, budget)
            except OSError as exc:
                log.debug("error talking to %s at %d baud: %s", port, baud, exc)
                first_error = first_error or (exc, port)
                conn = None
            if conn is not None:
                return conn
            _close_quietly(ser)
        if first_error is not None:
            error, port = first_error
            raise ConnectionError(self._error_message(error, port)) from error
        raise ConnectionError(
            user_message("no_reply", ", ".join(ports), self._platform, bauds=self._bauds)
        )

    def _handshake(
        self, ser: SerialLike, port: str, baud: int, budget_s: float
    ) -> _Connection | None:
        """Send ``C0`` and wait for ``#C2-M`` + ``#C2-F``; resend once if the device ignored it."""
        parser = protocol.Parser()
        model: ModelInfo | None = None
        config: DeviceConfig | None = None
        backlog: list[protocol.Event] = []
        start = time.monotonic()
        resent = False
        ser.write(protocol.request_config())
        while time.monotonic() - start < budget_s and not self._stop.is_set():
            data = ser.read(max(ser.in_waiting, 1))
            self._tap(data)
            for event in parser.feed(data):
                if config is not None:
                    backlog.append(event)
                elif isinstance(event, protocol.ModelReply):
                    model = event.model
                elif isinstance(event, protocol.ConfigReply) and model is not None:
                    config = event.config
            if model is not None and config is not None:
                return _Connection(ser, port, baud, parser, model, config, backlog)
            if not resent and model is None and time.monotonic() - start > budget_s / 2:
                log.debug("no #C2-M from %s at %d baud yet, resending C0", port, baud)
                ser.write(protocol.request_config())
                resent = True
        return None

    def _tap(self, data: bytes) -> None:
        if data and self._raw_sink is not None:
            self._raw_sink(data)

    def _adopt(self, conn: _Connection) -> None:
        with self._lock:
            self._model = conn.model
            self._config = conn.config
            self._active = (conn.port, conn.baud)
            self._capabilities = models.resolve(conn.model, conn.config)

    def _connected_message(self, conn: _Connection) -> str:
        caps = self._capabilities
        name = caps.main_name if caps is not None else "RF Explorer"
        return (
            f"Connected to {name} (firmware {conn.model.firmware}) on {conn.port} "
            f"at {conn.baud} baud"
        )

    def _error_message(self, exc: BaseException, port: str) -> str:
        kind = classify_error(exc, self._platform)
        return user_message(kind, port, self._platform, detail=str(exc))

    # --- worker thread ---

    def _run(self, conn: _Connection) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._pump(conn)
                except (OSError, _LinkLost) as exc:
                    _close_quietly(conn.serial)
                    log.info("lost RF Explorer on %s: %s", conn.port, exc)
                    put_drop_oldest(
                        self.events,
                        LinkEvent("disconnected", "RF Explorer disconnected; reconnecting..."),
                    )
                    new_conn = self._reconnect()
                    if new_conn is None:
                        return
                    conn = new_conn
                    self._adopt(conn)
                    put_drop_oldest(
                        self.events, LinkEvent("connected", self._connected_message(conn))
                    )
        finally:
            _close_quietly(conn.serial)

    def _reconnect(self) -> _Connection | None:
        for delay in backoff_delays(*self._backoff):
            if self._stop.wait(delay):
                return None
            try:
                return self._connect(self._connect_timeout)
            except ConnectionError as exc:
                log.debug("reconnect failed: %s", exc)
        return None  # pragma: no cover (backoff_delays never ends)

    def _pump(self, conn: _Connection) -> None:
        """Stream until ``close()``; raises ``OSError``/``_LinkLost`` when the device is lost.

        A command still unconfirmed when the previous connection was lost is re-sent first.
        """
        ser = conn.serial
        holding = False  # the handshake's C0 restarted the sweep dump
        sent_at = 0.0
        retried = False
        last_data = time.monotonic()  # also reset on every write: silence is timed from there
        for event in conn.backlog:
            self._handle(event)
        conn.backlog.clear()
        if self._in_flight is not None:
            log.debug("re-sending %s after reconnect", self._in_flight.kind)
            ser.write(self._in_flight.data)
            sent_at = last_data = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            in_flight = self._in_flight
            if in_flight is None:
                try:
                    cmd = self._commands.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if cmd.confirmed_by_config:
                        self._in_flight, sent_at, retried = cmd, now, False
                    ser.write(cmd.data)
                    last_data = now
                    if cmd.kind == "hold":
                        holding = True
                    elif cmd.confirmed_by_config:
                        # A new config resumes the sweep dump (seen on hardware for set_config;
                        # CJ is followed by a new dump too, F2).
                        holding = False
            elif now - sent_at > self._command_timeout:
                if not retried:
                    log.debug("no #C2-F after %s, resending", in_flight.kind)
                    ser.write(in_flight.data)
                    sent_at = last_data = now
                    retried = True
                else:
                    put_drop_oldest(
                        self.events,
                        LinkEvent("error", "RF Explorer did not confirm the new settings"),
                    )
                    self._in_flight = None

            data = ser.read(max(ser.in_waiting, 1))
            self._tap(data)
            if data:
                last_data = time.monotonic()
                for event in conn.parser.feed(data):
                    if self._handle(event):
                        self._in_flight = None
            elif not holding and time.monotonic() - last_data > self._stall_timeout:
                raise _LinkLost(f"no data for {self._stall_timeout:.0f} s")

    def _handle(self, event: protocol.Event) -> bool:
        """Apply one parser event; returns True for a config reply."""
        if isinstance(event, protocol.SweepData):
            config = self._config
            if config is not None and event.samples.shape == (config.sweep_points,):
                put_drop_oldest(
                    self.sweeps, protocol.make_sweep(config, event.samples, time.time())
                )
            return False
        if isinstance(event, protocol.ConfigReply):
            with self._lock:
                self._config = event.config
                if self._model is not None:
                    self._capabilities = models.resolve(self._model, event.config)
            return True
        if isinstance(event, protocol.ModelReply):
            with self._lock:
                self._model = event.model
                if self._config is not None:
                    self._capabilities = models.resolve(event.model, self._config)
            return False
        if isinstance(event, protocol.Unknown):
            log.debug("device: %s", event.line)
        return False


def _close_quietly(ser: SerialLike) -> None:
    try:
        ser.close()
    except OSError:
        log.debug("error closing serial port", exc_info=True)


__all__ = [
    "BAUD_RATES",
    "PortInfoLike",
    "SerialFactory",
    "SerialLike",
    "SerialLink",
    "SerialPort",
    "backoff_delays",
    "classify_error",
    "find_ports",
    "list_serial_ports",
    "open_serial",
    "user_message",
]
