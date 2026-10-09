"""Recording, replay and long-run logger orchestration for the UI (no Dear PyGui).

:class:`RecordingActions` hooks into the controller (``on_sweep`` / ``on_tick`` / ``on_shutdown``,
all on the UI thread) and owns:

* the **recorder**: while on, every live sweep (and every finished scan) is appended to a
  ``.ocrec`` file (:mod:`opencoord.io.recording`); the writer flushes chunks on its own schedule,
  and recording keeps going across a reconnect until stopped or the app exits;
* **replay** control of a connected :class:`~opencoord.device.replay.ReplayLink` (open a file =
  connect to ``replay:<path>`` through the normal connect path; speed; pause; position);
* the **logger**: a :class:`~opencoord.core.logger.LoggerEngine` fed with the sweeps the user sees
  (amplitude offset applied), writing CSV rows each interval and alert lines to a file, with a
  status-bar alert and a log line.

Failures never raise: they become a status message and a ``False`` result. Disk errors stop the
recording or logger concerned.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from opencoord.core import logger as logger_mod
from opencoord.core.logger import Alert, LoggerEngine, LogRange, LogWriter
from opencoord.core.types import Sweep
from opencoord.device.replay import replay_port
from opencoord.io import recording as rec
from opencoord.io.recording import RecordingError, RecordingInfo, RecordingWriter
from opencoord.ui.controller import Controller

log = logging.getLogger(__name__)

#: Replay speeds offered in the UI, label -> factor (``inf`` = as fast as the UI consumes).
SPEEDS: Final[dict[str, float]] = {"1x": 1.0, "4x": 4.0, "Max": math.inf}
_MHZ = 1_000_000


def speed_label(speed: float) -> str:
    return next((label for label, v in SPEEDS.items() if v == speed), "1x")


def default_recording_name() -> str:
    return f"opencoord-{datetime.now():%Y%m%d-%H%M%S}{rec.RECORDING_SUFFIX}"


def default_log_name() -> str:
    return f"opencoord-log-{datetime.now():%Y%m%d}.csv"


@dataclass(frozen=True)
class RecordingStatus:
    active: bool
    path: Path | None
    elapsed_s: float
    sweeps: int


@dataclass(frozen=True)
class ReplayStatus:
    name: str
    speed: float
    paused: bool
    position: float
    total_sweeps: int
    ended: bool


@dataclass(frozen=True)
class LoggerStatus:
    enabled: bool
    path: Path | None
    next_row_in_s: float | None
    rows_written: int
    alerts: int


def _iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RecordingActions:
    def __init__(
        self,
        controller: Controller,
        *,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], str] = _iso,
    ) -> None:
        self.controller = controller
        self._clock = clock
        self._timestamp = timestamp
        # recorder
        self._writer: RecordingWriter | None = None
        self._rec_started = 0.0
        # logger
        self.logger_ranges: list[LogRange] = []
        self.logger_interval_s: float = logger_mod.DEFAULT_INTERVAL_S
        #: Own alert threshold (dBm); ``None`` follows the threshold line of the Markers tab.
        self.logger_threshold_dbm: float | None = None
        self._engine: LoggerEngine | None = None
        self._log_writer: LogWriter | None = None
        self._rows_written = 0
        self._alerts = 0
        controller.on_sweep.append(self._on_sweep)
        controller.on_tick.append(self._on_tick)
        controller.on_shutdown.append(self.shutdown)

    # --- helpers ------------------------------------------------------------------------------

    def say(self, message: str) -> None:
        st = self.controller.state
        st.message = message
        st.ui_version += 1

    # --- recorder -----------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._writer is not None

    def recording_status(self) -> RecordingStatus:
        w = self._writer
        if w is None:
            return RecordingStatus(False, None, 0.0, 0)
        return RecordingStatus(True, w.path, self._clock() - self._rec_started, w.sweep_count)

    def _device_info(self) -> RecordingInfo | None:
        st = self.controller.state
        model, config, caps = st.model, st.config, st.capabilities
        if model is None or config is None or caps is None:
            return None
        expansion = config.expansion_active and model.expansion_code is not None
        code = (
            model.expansion_code
            if expansion and model.expansion_code is not None
            else model.main_code
        )
        return RecordingInfo(
            model_name=caps.name,
            model_code=code,
            expansion_code=None,
            firmware=model.firmware,
            min_hz=config.min_hz,
            max_hz=config.max_hz,
            amp_top_dbm=config.amp_top_dbm,
            amp_bottom_dbm=config.amp_bottom_dbm,
        )

    def start_recording(self, path: Path) -> bool:
        st = self.controller.state
        if self._writer is not None:
            self.say("Already recording")
            return False
        if st.connection != "connected":
            self.say("Connect a device (or open a recording) before recording")
            return False
        path = rec.with_suffix(path)
        try:
            self._writer = RecordingWriter(path, self._device_info(), clock=self._clock)
        except (RecordingError, OSError) as exc:
            self.say(f"Cannot record to {path.name}: {getattr(exc, 'strerror', None) or exc}")
            return False
        self._rec_started = self._clock()
        st.ui_version += 1
        suffix = "" if st.running else " (sweeps are recorded while Live or a scan runs)"
        self.say(f"Recording to {path}{suffix}")
        return True

    def stop_recording(self) -> bool:
        """Finish the file; ``False`` if nothing was recording or it could not be written."""
        writer, self._writer = self._writer, None
        if writer is None:
            return False
        count = writer.sweep_count
        try:
            path = writer.close()
        except (RecordingError, OSError) as exc:
            self.say(
                f"Could not finish {writer.path.name}: {getattr(exc, 'strerror', None) or exc}. "
                f"The flushed data is in {rec.parts_dir_for(writer.path).name} and can be opened"
            )
            return False
        self.say(f"Saved recording {path} ({count} sweeps)")
        return True

    def _fail_recording(self, exc: Exception) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        reason = getattr(exc, "strerror", None) or exc
        self.say(
            f"Recording stopped: {reason}. Flushed data is in "
            f"{rec.parts_dir_for(writer.path).name} and can be opened for replay"
        )
        log.warning("recording stopped", exc_info=exc)

    # --- replay -------------------------------------------------------------------------------

    def open_replay(self, path: Path) -> bool:
        """Connect to a recording like to a port; refused while a connection exists."""
        if self.controller.state.connection != "disconnected":
            self.say("Disconnect before opening a recording")
            return False
        self.controller.connect(replay_port(path))
        return True

    def replay_status(self) -> ReplayStatus | None:
        link = self.controller.replay_link
        if link is None or not link.is_open:
            return None
        return ReplayStatus(
            link.path.name, link.speed, link.paused, link.position, link.total_sweeps, link.ended
        )

    def set_replay_speed(self, label: str) -> None:
        link = self.controller.replay_link
        if link is not None and label in SPEEDS:
            link.set_speed(SPEEDS[label])
            self.controller.state.ui_version += 1

    def toggle_replay_pause(self) -> None:
        """Play from the start / resume (when stopped or ended), else pause or resume."""
        c = self.controller
        link = c.replay_link
        if link is None:
            return
        if not c.state.running:
            c.start()
        else:
            link.set_paused(not link.paused)
        c.state.ui_version += 1

    def restart_replay(self) -> None:
        c = self.controller
        link = c.replay_link
        if link is None:
            return
        link.rewind()
        c.state.traces.reset()
        c.state.waterfall.clear()
        c.state.trace_version += 1
        if c.state.running:
            link.set_paused(False)
        c.state.ui_version += 1

    # --- logger -------------------------------------------------------------------------------

    @property
    def logger_enabled(self) -> bool:
        return self._engine is not None

    def logger_status(self) -> LoggerStatus:
        e = self._engine
        if e is None or self._log_writer is None:
            return LoggerStatus(False, None, None, 0, 0)
        remaining = max(0.0, e.interval_s - (self._clock() - e.last_write))
        return LoggerStatus(
            True, self._log_writer.path, remaining, self._rows_written, self._alerts
        )

    def add_logger_range(self, start_hz: float, stop_hz: float) -> bool:
        if self._engine is not None:
            self.say("Stop the logger to change its ranges")
            return False
        lo, hi = sorted((round(start_hz), round(stop_hz)))
        if lo >= hi:
            self.say("The range must have a start below its stop")
            return False
        if len(self.logger_ranges) >= logger_mod.MAX_RANGES:
            self.say(f"At most {logger_mod.MAX_RANGES} logger ranges")
            return False
        self.logger_ranges.append(LogRange(lo, hi))
        self.controller.state.ui_version += 1
        return True

    def add_view_range(self) -> bool:
        lo, hi = self.controller.state.view_range_hz
        return self.add_logger_range(lo, hi)

    def remove_logger_range(self, index: int) -> None:
        if self._engine is not None:
            self.say("Stop the logger to change its ranges")
            return
        if 0 <= index < len(self.logger_ranges):
            del self.logger_ranges[index]
            self.controller.state.ui_version += 1

    def set_logger_interval(self, seconds: float) -> bool:
        if not seconds >= logger_mod.MIN_INTERVAL_S:
            self.say(f"The logging interval must be at least {logger_mod.MIN_INTERVAL_S:.0f} s")
            return False
        self.logger_interval_s = float(seconds)
        if self._engine is not None:
            self._engine.interval_s = float(seconds)
        self.controller.state.ui_version += 1
        return True

    def set_logger_threshold(self, value: float | None) -> None:
        """Own alert threshold in dBm, or ``None`` to follow the threshold line."""
        if value is not None and not math.isfinite(value):
            return
        self.logger_threshold_dbm = value
        self.controller.state.ui_version += 1

    def effective_threshold(self) -> float | None:
        if self.logger_threshold_dbm is not None:
            return self.logger_threshold_dbm
        return self.controller.state.threshold_dbm

    def enable_logger(self, path: Path) -> bool:
        """Start logging to ``path`` (appended); with no ranges the current view range is used."""
        if self._engine is not None:
            self.say("The logger is already running")
            return False
        if not self.logger_ranges and not self.add_view_range():
            return False
        try:
            engine = LoggerEngine(
                self.logger_ranges,
                interval_s=self.logger_interval_s,
                threshold_dbm=self.effective_threshold(),
            )
            self._log_writer = LogWriter(path)
        except (ValueError, OSError) as exc:
            self.say(f"Cannot start the logger: {getattr(exc, 'strerror', None) or exc}")
            return False
        engine.start(self._clock())
        self._engine = engine
        self._rows_written = self._alerts = 0
        st = self.controller.state
        st.logger_alert = None
        st.ui_version += 1
        self.say(
            f"Logging {len(self.logger_ranges)} range(s) every {engine.interval_s:g} s to {path}"
            " (while Live or scanning)"
        )
        return True

    def disable_logger(self) -> None:
        """Stop logging; the maximum collected in the unfinished interval is written first."""
        engine, writer = self._engine, self._log_writer
        if engine is None or writer is None:
            return
        try:
            for row in engine.take_rows(self._clock(), self._timestamp()):
                writer.write_line(logger_mod.row_csv(row))
        except OSError:
            log.warning("could not write the last logger rows", exc_info=True)
        writer.close()
        self._engine = self._log_writer = None
        self.controller.state.ui_version += 1
        self.say("Logger stopped")

    def clear_alert(self) -> None:
        self.controller.state.logger_alert = None
        self.controller.state.ui_version += 1

    def _alert(self, alert: Alert) -> None:
        text = (
            f"ALERT {alert.peak_hz / _MHZ:.3f} MHz at {alert.level_dbm:.1f} dBm exceeds "
            f"{alert.threshold_dbm:.1f} dBm "
            f"(range {alert.start_hz / _MHZ:.3f}-{alert.stop_hz / _MHZ:.3f} MHz)"
        )
        log.warning("logger %s", text)
        self._alerts += 1
        self.controller.state.logger_alert = text
        self.say(text)
        if self._log_writer is not None:
            self._log_writer.write_line(logger_mod.alert_csv(self._timestamp(), alert))

    def _fail_logger(self, exc: Exception) -> None:
        log.warning("logger stopped", exc_info=exc)
        writer, self._engine, self._log_writer = self._log_writer, None, None
        if writer is not None:
            writer.close()
        self.say(f"Logger stopped: {getattr(exc, 'strerror', None) or exc}")

    # --- controller hooks ---------------------------------------------------------------------

    def _on_sweep(self, raw: Sweep, shown: Sweep) -> None:
        writer = self._writer
        if writer is not None:
            try:
                writer.append(raw)
            except (RecordingError, OSError) as exc:
                self._fail_recording(exc)
        engine = self._engine
        if engine is not None:
            engine.set_threshold(self.effective_threshold())
            try:
                for alert in engine.feed(shown.freqs_hz, shown.dbm, self._clock()):
                    self._alert(alert)
            except OSError as exc:
                self._fail_logger(exc)

    def _on_tick(self, _now: float) -> None:
        writer = self._writer
        if writer is not None:
            try:
                writer.poll()
            except (RecordingError, OSError) as exc:
                self._fail_recording(exc)
        engine, log_writer = self._engine, self._log_writer
        if engine is not None and log_writer is not None:
            now = self._clock()
            if engine.due(now):
                try:
                    for row in engine.take_rows(now, self._timestamp()):
                        log_writer.write_line(logger_mod.row_csv(row))
                        self._rows_written += 1
                except OSError as exc:
                    self._fail_logger(exc)

    def shutdown(self) -> None:
        """Finish the recording and the log (app exit)."""
        self.stop_recording()
        self.disable_logger()


__all__ = [
    "SPEEDS",
    "LoggerStatus",
    "RecordingActions",
    "RecordingStatus",
    "ReplayStatus",
    "default_log_name",
    "default_recording_name",
    "speed_label",
]
