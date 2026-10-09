"""Simulated RF Explorer WSUB1G+ for tests, CI and ``opencoord --simulator``.

``generate`` is a pure, deterministic spectrum model; ``SimulatedLink`` wraps it in a thread that
behaves like the real link (same ``Link`` interface, ~10 sweeps per second).
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import numpy.typing as npt

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep
from opencoord.device import models
from opencoord.device.link_api import LinkEvent, put_drop_oldest
from opencoord.device.models import Capabilities
from opencoord.device.protocol import make_sweep

_MHZ = 1_000_000
NOISE_FLOOR_DBM = -105.0
DVBT_LEVEL_DBM = -60.0
DVBT_WIDTH_HZ = 8 * _MHZ
# EU UHF channel n (21..69) is centred on 474 MHz + 8 MHz * (n - 21).
DVBT_CHANNELS = (22, 27, 35)
# (centre Hz, peak dBm): narrowband FM carriers (wireless mics) in 470-700 and 823-832 MHz.
CARRIERS: tuple[tuple[int, float], ...] = (
    (563_300_000, -48.0),
    (607_100_000, -55.0),
    (671_900_000, -52.0),
    (823_600_000, -50.0),
    (826_400_000, -58.0),
    (830_100_000, -45.0),
)
INTERMITTENT_HZ = 614_500_000
INTERMITTENT_DBM = -50.0
INTERMITTENT_PERIOD_S = 4.0  # on for the first half of each period
_CARRIER_HALF_WIDTH_HZ = 150_000.0
_NOISE_JITTER_DB = 1.5
_DEFAULT_POINTS = 112
_QUEUE_SIZE = 64


def _dvbt_centre_hz(channel: int) -> int:
    return (474 + 8 * (channel - 21)) * _MHZ


def _to_mw(dbm: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return np.power(10.0, dbm / 10.0)


def generate(
    start_hz: int, stop_hz: int, points: int, t: float, seed: int
) -> npt.NDArray[np.float32]:
    """Synthetic spectrum in dBm at ``points`` evenly spaced frequencies from start to stop.

    Deterministic in all arguments. ``t`` is seconds since the start of the simulation.
    """
    freqs = np.linspace(start_hz, stop_hz, points, dtype=np.float64)
    step = (stop_hz - start_hz) / max(points - 1, 1)
    rng = np.random.default_rng([seed, round(t * 1000)])
    power = _to_mw(NOISE_FLOOR_DBM + rng.normal(0.0, _NOISE_JITTER_DB, points))

    for ch in DVBT_CHANNELS:
        inside = np.abs(freqs - _dvbt_centre_hz(ch)) <= DVBT_WIDTH_HZ / 2
        ripple = rng.normal(0.0, 0.7, points)
        power += np.where(inside, _to_mw(DVBT_LEVEL_DBM + ripple), 0.0)

    carriers = list(CARRIERS)
    if (t % INTERMITTENT_PERIOD_S) < INTERMITTENT_PERIOD_S / 2:
        carriers.append((INTERMITTENT_HZ, INTERMITTENT_DBM))
    # A carrier is narrower than a coarse bin, so widen its bump to roughly one step (as the
    # device's RBW does) to keep it visible at low point density.
    half_width = max(_CARRIER_HALF_WIDTH_HZ, 0.75 * step)
    for centre, peak in carriers:
        x = (freqs - centre) / half_width
        power += 10.0 ** (peak / 10.0) * np.exp(-0.5 * x * x * 4.0)

    return (10.0 * np.log10(power)).astype(np.float32)


class SimulatedLink:
    """``Link`` implementation emitting synthetic WSUB1G+ sweeps from a daemon thread."""

    def __init__(
        self,
        seed: int = 0,
        *,
        sweep_points: int = _DEFAULT_POINTS,
        sweep_interval_s: float = 0.1,
        queue_size: int = _QUEUE_SIZE,
        connect_delay_s: float = 0.0,
    ) -> None:
        if sweep_points < 2:
            raise ValueError("sweep_points must be at least 2")
        self._seed = seed
        self._connect_delay = connect_delay_s
        self._points = sweep_points
        self._interval = sweep_interval_s
        self.sweeps: queue.Queue[Sweep] = queue.Queue(maxsize=queue_size)
        self.events: queue.Queue[LinkEvent] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._holding = threading.Event()
        self._thread: threading.Thread | None = None
        self._model: ModelInfo | None = None
        self._config: DeviceConfig | None = None
        self._capabilities: Capabilities | None = None
        self._ready = threading.Event()
        self._pending: tuple[int, int] | None = None

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

    def open(self, timeout_s: float = 5.0) -> None:
        if self.is_open:
            return
        self._stop.clear()
        self._holding.clear()
        self._ready.clear()
        self._pending = None
        self._thread = threading.Thread(target=self._run, name="simulated-link", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            message = "Simulated RF Explorer did not report its configuration in time"
            self._stop.set()
            self._thread.join(timeout=2.0)
            if not self._thread.is_alive():
                self._thread = None
            put_drop_oldest(self.events, LinkEvent("error", message))
            raise ConnectionError(message)
        put_drop_oldest(
            self.events, LinkEvent("connected", "Simulated RF Explorer WSUB1G+ connected")
        )

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=2.0)
        if thread.is_alive():  # still running: stay "open" rather than lie
            return
        self._thread = None
        put_drop_oldest(self.events, LinkEvent("disconnected", "Simulator closed"))

    def set_span(self, start_hz: int, stop_hz: int) -> None:
        if stop_hz <= start_hz:
            raise ValueError("stop must be greater than start")
        caps = self._capabilities
        if not self.is_open or caps is None:
            raise RuntimeError("simulator is not open")
        min_span = self._points - 1  # at least 1 Hz per step
        start = max(start_hz, caps.min_hz)
        stop = min(stop_hz, caps.max_hz)
        if stop - start > caps.max_span_hz:
            stop = start + caps.max_span_hz
        if stop - start < min_span:
            stop = min(start + min_span, caps.max_hz)
            start = stop - min_span
        with self._lock:
            self._pending = (start, stop)
        self._holding.clear()

    def hold(self) -> None:
        self._holding.set()

    def switch_module(self, main: bool) -> None:
        if main:
            return
        put_drop_oldest(self.events, LinkEvent("error", "This device has no expansion module"))

    def _make_config(self, start_hz: int, stop_hz: int) -> DeviceConfig:
        hint = models.MODELS[10]
        step = round((stop_hz - start_hz) / (self._points - 1))
        return DeviceConfig(
            start_hz=start_hz,
            step_hz=step,
            amp_top_dbm=-30.0,
            amp_bottom_dbm=-120.0,
            sweep_points=self._points,
            expansion_active=False,
            mode=0,
            min_hz=hint.min_hz,
            max_hz=hint.max_hz,
            max_span_hz=hint.max_hz - hint.min_hz,
            rbw_hz=None,
            amp_offset_db=0.0,
            calculator_mode=0,
        )

    def _run(self) -> None:
        # Model and config arrive from the "device" on the worker thread, as with the real link.
        if self._stop.wait(self._connect_delay):
            return
        hint = models.MODELS[10]
        model = ModelInfo(main_code=10, expansion_code=None, firmware="03.39")
        config = self._make_config(hint.min_hz, hint.max_hz)
        with self._lock:
            self._model = model
            self._config = config
            self._capabilities = models.resolve(model, config)
        self._ready.set()
        t0 = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                pending, self._pending = self._pending, None
                if pending is not None:
                    self._config = self._make_config(*pending)  # the device "echoes" #C2-F
                config = self._config
            if not self._holding.is_set():
                assert config is not None
                now = time.monotonic() - t0
                samples = generate(
                    config.start_hz, config.stop_hz, config.sweep_points, now, self._seed
                )
                put_drop_oldest(self.sweeps, make_sweep(config, samples, time.time()))
            self._stop.wait(self._interval)


__all__ = ["SimulatedLink", "generate"]
