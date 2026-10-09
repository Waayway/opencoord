"""``ReplayLink``: plays a recording (``.ocrec``) back through the ``Link`` interface.

The link cannot retune: ``set_span``, ``hold``, ``switch_module`` and ``set_sweep_points`` raise
``NotImplementedError`` and ``retunable`` is ``False``. It starts paused; the controller (or the
Record tab) calls :meth:`ReplayLink.set_paused` / :meth:`~ReplayLink.set_speed`.

Timing lives in the pure :class:`ReplayScheduler` (driven by an explicit ``now``, so tests use a
fake clock): sweeps are emitted at the recorded intervals divided by the speed (1x / 4x / max =
``math.inf``). Gaps longer than :data:`MAX_GAP_S` (the user stopped acquiring for a while) are
shortened to that, and after a stall the schedule restarts from "now" instead of bursting.

Deviations from the usual ``Link`` contract: the ``sweeps`` queue applies **backpressure**
(nothing is dropped; the replay waits for the consumer) because a recording must arrive complete,
and ``config`` is synthesised from the axis of the last emitted sweep (replaced only when the axis
changes). At the end of the recording the link emits ``LinkEvent("disconnected", "End of
recording")`` once and stays open (``ended``); :meth:`ReplayLink.rewind` starts over.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from opencoord.core.types import DeviceConfig, ModelInfo, Sweep
from opencoord.device import models
from opencoord.device.link_api import LinkEvent, put_drop_oldest
from opencoord.device.models import Capabilities
from opencoord.io.recording import RecordingError, RecordingInfo, RecordingReader

log = logging.getLogger(__name__)

#: ``port`` strings starting with this select a replay file (``replay:/path/to/x.ocrec``).
REPLAY_PREFIX: Final = "replay:"
#: Longest recorded gap between two sweeps that is waited out (seconds at 1x).
MAX_GAP_S: Final = 2.0
#: A late schedule older than this is restarted from "now" (no burst after a stall).
_RESYNC_S: Final = 1.0
_BATCH: Final = 64
_QUEUE_SIZE: Final = 256
_IDLE_WAIT_S: Final = 0.1
_BUSY_WAIT_S: Final = 0.01
_UNKNOWN_MODEL: Final = 255
END_MESSAGE: Final = "End of recording"


def replay_port(path: Path) -> str:
    return f"{REPLAY_PREFIX}{path}"


def replay_path(port: str | None) -> Path | None:
    """The file named by a ``replay:`` port string, else ``None``."""
    if port is None or not port.startswith(REPLAY_PREFIX):
        return None
    return Path(port[len(REPLAY_PREFIX) :])


class ReplayScheduler:
    """When to emit which recorded sweep (pure: time is passed in)."""

    def __init__(self, sweeps: Iterator[Sweep], total: int) -> None:
        self._it = sweeps
        self.total = total
        self.emitted = 0
        self.speed = 1.0
        self.paused = True
        self._next: Sweep | None = next(self._it, None)
        self._due = 0.0
        #: Seconds (wall) still to wait for ``_next``, kept while paused.
        self._remaining = 0.0

    @property
    def upcoming(self) -> Sweep | None:
        """The next sweep to be emitted (``None`` once finished)."""
        return self._next

    @property
    def finished(self) -> bool:
        return self._next is None

    @property
    def position(self) -> float:
        """Fraction of the recording emitted, 0..1 (1 for an empty one)."""
        return min(self.emitted / self.total, 1.0) if self.total > 0 else 1.0

    def set_paused(self, paused: bool, now: float) -> None:
        if paused == self.paused:
            return
        if paused:
            self._remaining = max(0.0, self._due - now)
        else:
            self._due = now + self._remaining
        self.paused = paused

    def set_speed(self, speed: float, now: float) -> None:
        if not speed > 0:
            raise ValueError("speed must be positive")
        old = self.speed
        remaining = self._remaining if self.paused else max(0.0, self._due - now)
        if math.isinf(old) or math.isinf(speed):
            remaining = 0.0
        else:
            remaining *= old / speed
        self.speed = speed
        if self.paused:
            self._remaining = remaining
        else:
            self._due = now + remaining

    def wait_s(self, now: float) -> float | None:
        """Seconds until the next sweep is due; ``None`` when paused or finished."""
        if self.paused or self._next is None:
            return None
        return max(0.0, self._due - now)

    def due(self, now: float, limit: int = _BATCH) -> list[Sweep]:
        """The sweeps due at ``now`` (at most ``limit``), in order."""
        out: list[Sweep] = []
        while not self.paused and self._next is not None and now >= self._due and len(out) < limit:
            sweep = self._next
            out.append(sweep)
            self.emitted += 1
            self._next = next(self._it, None)
            if self._next is None:
                break
            self._due += self._gap(sweep, self._next)
            if now - self._due > _RESYNC_S:
                self._due = now
        return out

    def _gap(self, prev: Sweep, nxt: Sweep) -> float:
        if math.isinf(self.speed):
            return 0.0
        gap = nxt.timestamp - prev.timestamp
        if not math.isfinite(gap) or gap <= 0:
            return 0.0
        return min(gap, MAX_GAP_S) / self.speed

    def reset(self, sweeps: Iterator[Sweep], now: float) -> None:
        """Start over with a fresh iterator (paused, speed kept)."""
        self._it = sweeps
        self._next = next(self._it, None)
        self.emitted = 0
        self.paused = True
        self._due = now
        self._remaining = 0.0


class ReplayLink:
    """``Link`` implementation that plays a recording."""

    #: A recording cannot be retuned; scanning is not available.
    retunable = False

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
        queue_size: int = _QUEUE_SIZE,
    ) -> None:
        self.path = path
        self._clock = clock
        self._threaded = threaded
        self._queue_size = queue_size
        self.sweeps: queue.Queue[Sweep] = queue.Queue(maxsize=queue_size)
        self.events: queue.Queue[LinkEvent] = queue.Queue(maxsize=64)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._opened = False
        self._reader: RecordingReader | None = None
        self._scheduler: ReplayScheduler | None = None
        self._device: RecordingInfo | None = None
        self._model: ModelInfo | None = None
        self._config: DeviceConfig | None = None
        self._capabilities: Capabilities | None = None
        self._ended = False

    # --- Link ---------------------------------------------------------------------------------

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
        return self._opened

    def open(self, timeout_s: float = 5.0) -> None:
        if self._opened:
            return
        try:
            reader = RecordingReader.open(self.path)
            if reader.total_sweeps == 0:
                raise RecordingError(f"{self.path.name} contains no sweeps")
            scheduler = ReplayScheduler(reader.sweeps(), reader.total_sweeps)
            first = scheduler.upcoming
            if first is None:
                raise RecordingError(f"{self.path.name} contains no sweeps")
        except RecordingError as exc:
            message = str(exc)
            put_drop_oldest(self.events, LinkEvent("error", message))
            raise ConnectionError(message) from exc
        with self._lock:
            self._reader, self._scheduler = reader, scheduler
            self._device = reader.meta.device
            self._ended = False
            self._model = self._model_info()
            self._apply_axis(first)
        self._stop.clear()
        self._opened = True
        if self._threaded:
            self._thread = threading.Thread(target=self._run, name="replay-link", daemon=True)
            self._thread.start()
        put_drop_oldest(
            self.events,
            LinkEvent("connected", f"Replaying {self.path.name} ({reader.total_sweeps} sweeps)"),
        )

    def close(self) -> None:
        if not self._opened:
            return
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():
                return
        self._thread = None
        self._opened = False
        put_drop_oldest(self.events, LinkEvent("disconnected", "Replay closed"))

    def set_span(self, start_hz: int, stop_hz: int) -> None:
        raise NotImplementedError("A recording cannot be retuned")

    def set_sweep_points(self, points: int) -> None:
        raise NotImplementedError("A recording cannot be retuned")

    def hold(self) -> None:
        raise NotImplementedError("Use set_paused to pause a replay")

    def switch_module(self, main: bool) -> None:
        raise NotImplementedError("A recording has no expansion module")

    # --- replay control -----------------------------------------------------------------------

    @property
    def position(self) -> float:
        """Fraction of the recording played, 0..1."""
        with self._lock:
            return self._scheduler.position if self._scheduler else 0.0

    @property
    def total_sweeps(self) -> int:
        return self._scheduler.total if self._scheduler else 0

    @property
    def paused(self) -> bool:
        return self._scheduler.paused if self._scheduler else True

    @property
    def speed(self) -> float:
        return self._scheduler.speed if self._scheduler else 1.0

    @property
    def ended(self) -> bool:
        return self._ended

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            if self._scheduler is not None:
                self._scheduler.set_paused(paused, self._clock())
        self._wake.set()

    def set_speed(self, speed: float) -> None:
        with self._lock:
            if self._scheduler is not None:
                self._scheduler.set_speed(speed, self._clock())
        self._wake.set()

    def rewind(self) -> None:
        """Start again from the first sweep (paused)."""
        with self._lock:
            if self._reader is None or self._scheduler is None:
                return
            self._scheduler.reset(self._reader.sweeps(), self._clock())
            self._ended = False
        self._wake.set()

    # --- internals ----------------------------------------------------------------------------

    def _model_info(self) -> ModelInfo:
        d = self._device
        if d is None:
            return ModelInfo(_UNKNOWN_MODEL, None, "")
        return ModelInfo(d.model_code, d.expansion_code, d.firmware)

    def _apply_axis(self, sweep: Sweep) -> None:
        """Replace ``config`` (and the capabilities) if ``sweep`` has another axis than the last."""
        n = len(sweep.dbm)
        step = max(1, round((sweep.stop_hz - sweep.start_hz) / (n - 1))) if n > 1 else 1
        cfg = self._config
        if cfg is not None and (cfg.start_hz, cfg.step_hz, cfg.sweep_points) == (
            sweep.start_hz,
            step,
            n,
        ):
            return
        d = self._device
        lo = min(d.min_hz, sweep.start_hz) if d else sweep.start_hz
        hi = max(d.max_hz, sweep.stop_hz) if d else sweep.stop_hz
        config = DeviceConfig(
            start_hz=sweep.start_hz,
            step_hz=step,
            amp_top_dbm=d.amp_top_dbm if d else -30.0,
            amp_bottom_dbm=d.amp_bottom_dbm if d else -120.0,
            sweep_points=n,
            expansion_active=False,
            mode=0,
            min_hz=lo,
            max_hz=hi,
            max_span_hz=hi - lo,
            rbw_hz=None,
            amp_offset_db=0.0,  # the recorded levels already include the device's offset
            calculator_mode=0,
        )
        assert self._model is not None
        self._config = config
        self._capabilities = models.resolve(self._model, config)

    def pump(self) -> float:
        """Emit what is due now; returns how long to wait before calling again (seconds)."""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None or self._ended:
                return _IDLE_WAIT_S
            free = self._queue_size - self.sweeps.qsize()  # only this method adds to the queue
            try:
                due = scheduler.due(self._clock(), limit=min(_BATCH, free)) if free > 0 else []
            except RecordingError as exc:
                log.warning("replay stopped: %s", exc)
                put_drop_oldest(self.events, LinkEvent("error", str(exc)))
                self._end("Recording ended early: unreadable data")
                return _IDLE_WAIT_S
            for sweep in due:
                self._apply_axis(sweep)
                self.sweeps.put_nowait(sweep)
            if scheduler.finished and not scheduler.paused:
                self._end(END_MESSAGE)
                return _IDLE_WAIT_S
            if free <= 0:
                return _BUSY_WAIT_S
            wait = scheduler.wait_s(self._clock())
            if wait is None:
                return _IDLE_WAIT_S
            return _BUSY_WAIT_S if len(due) >= min(_BATCH, free) else min(wait, _IDLE_WAIT_S)

    def _end(self, message: str) -> None:
        self._ended = True
        put_drop_oldest(self.events, LinkEvent("disconnected", message))

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = self.pump()
            self._wake.wait(wait)
            self._wake.clear()


__all__ = [
    "END_MESSAGE",
    "REPLAY_PREFIX",
    "ReplayLink",
    "ReplayScheduler",
    "replay_path",
    "replay_port",
]
