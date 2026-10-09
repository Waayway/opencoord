"""Record tab: recording, replay and the long-run logger.

All behaviour lives in :class:`opencoord.ui.recording.RecordingActions`; this panel renders its
status each frame (cells are only rewritten when their text changed) and forwards clicks.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import dearpygui.dearpygui as dpg

from opencoord.core.logger import DEFAULT_INTERVAL_S, MAX_RANGES, MIN_INTERVAL_S, LogRange
from opencoord.io.recording import RECORDING_SUFFIX
from opencoord.ui import theme
from opencoord.ui.controller import Controller
from opencoord.ui.recording import (
    SPEEDS,
    RecordingActions,
    default_log_name,
    default_recording_name,
    speed_label,
)
from opencoord.ui.state import AppState

#: ``ask(title, extensions, action, default_name)``: a file dialog (see ``FileUI.ask``).
AskFile = Callable[..., None]

TAG_REC_PATH = "record.path"
TAG_REC_BUTTON = "record.button"
TAG_REC_STATUS = "record.status"
TAG_REPLAY_OPEN = "replay.open"
TAG_REPLAY_UNFINISHED = "replay.unfinished"
TAG_RECOVER = "record.recover"
TAG_REPLAY_INFO = "replay.info"
TAG_REPLAY_SPEED = "replay.speed"
TAG_REPLAY_PLAY = "replay.play"
TAG_REPLAY_RESTART = "replay.restart"
TAG_REPLAY_POSITION = "replay.position"
TAG_LOG_ENABLE = "logger.enable"
TAG_LOG_PATH = "logger.path"
TAG_LOG_INTERVAL = "logger.interval"
TAG_LOG_OWN = "logger.own_threshold"
TAG_LOG_THRESHOLD = "logger.threshold"
TAG_LOG_STATUS = "logger.status"
TAG_LOG_NEW_START = "logger.new_start"
TAG_LOG_NEW_STOP = "logger.new_stop"
TAG_LOG_ALERT = "logger.alert"
_MHZ = 1e6


def clock_text(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


def range_text(r: LogRange) -> str:
    return f"{r.start_hz / _MHZ:.3f} - {r.stop_hz / _MHZ:.3f} MHz"


class RecordPanel:
    def __init__(self, controller: Controller, actions: RecordingActions, ask: AskFile) -> None:
        self._c = controller
        self._a = actions
        self._ask = ask
        self._text: dict[str, str] = {}
        self._configured: dict[str, str] = {}
        self._values: dict[str, float] = {}
        self._ranges_version = -1

    @property
    def text_inputs(self) -> list[str]:
        return [
            TAG_REC_PATH,
            TAG_LOG_PATH,
            TAG_LOG_INTERVAL,
            TAG_LOG_THRESHOLD,
            TAG_LOG_NEW_START,
            TAG_LOG_NEW_STOP,
        ]

    def _set(self, tag: str, text: str) -> None:
        if self._text.get(tag) != text:
            self._text[tag] = text
            dpg.set_value(tag, text)

    def _cfg(self, tag: str, **kwargs: object) -> None:
        """``configure_item`` only when the settings differ from the last call for ``tag``."""
        key = repr(sorted(kwargs.items()))
        if self._configured.get(tag) != key:
            self._configured[tag] = key
            dpg.configure_item(tag, **kwargs)

    def _value(self, tag: str, value: float) -> None:
        if self._values.get(tag) != value:
            self._values[tag] = value
            dpg.set_value(tag, value)

    # --- callbacks ---

    def _toggle_record(self) -> None:
        if self._a.recording:
            self._a.stop_recording()
            dpg.set_value(TAG_REC_PATH, default_recording_name())  # the name is taken now
        else:
            self._a.start_recording(Path(str(dpg.get_value(TAG_REC_PATH)).strip() or "."))
            if self._a.recording:
                dpg.set_value(TAG_REC_PATH, str(self._a.recording_status().path))
            self._rotate_name_if_free()

    def _rotate_name_if_free(self) -> None:
        if not self._a.recording and not str(dpg.get_value(TAG_REC_PATH)).strip():
            dpg.set_value(TAG_REC_PATH, default_recording_name())

    def _choose_record_path(self) -> None:
        self._ask(
            "Record to",
            [RECORDING_SUFFIX],
            lambda p: dpg.set_value(TAG_REC_PATH, str(p)),
            default_name=Path(str(dpg.get_value(TAG_REC_PATH))).name,
        )

    def _open_replay(self) -> None:
        self._ask("Open recording", [RECORDING_SUFFIX, ".*"], self._a.open_replay)

    def _open_unfinished(self) -> None:
        self._ask(
            "Open unfinished recording (.ocrec.parts folder)",
            [],
            self._a.open_replay,
            directory=True,
        )

    def _choose_log_path(self) -> None:
        self._ask(
            "Log to",
            [".csv"],
            lambda p: dpg.set_value(TAG_LOG_PATH, str(p)),
            default_name=Path(str(dpg.get_value(TAG_LOG_PATH))).name,
        )

    def _on_enable(self, _sender: object, value: bool) -> None:
        if value:
            self._a.enable_logger(Path(str(dpg.get_value(TAG_LOG_PATH)).strip() or "."))
        else:
            self._a.disable_logger()

    def _on_interval(self, *_: object) -> None:
        self._a.set_logger_interval(float(dpg.get_value(TAG_LOG_INTERVAL)))
        self._ranges_version = -1

    def _on_threshold(self, *_: object) -> None:
        own = bool(dpg.get_value(TAG_LOG_OWN))
        self._a.set_logger_threshold(float(dpg.get_value(TAG_LOG_THRESHOLD)) if own else None)

    def _add_range(self) -> None:
        self._a.add_logger_range(
            float(dpg.get_value(TAG_LOG_NEW_START)) * _MHZ,
            float(dpg.get_value(TAG_LOG_NEW_STOP)) * _MHZ,
        )

    # --- layout ---

    def build(self) -> None:
        a = self._a
        dpg.add_text("Recording")
        with dpg.group(horizontal=True):
            dpg.add_input_text(tag=TAG_REC_PATH, default_value=default_recording_name(), width=-90)
            dpg.add_button(label="Choose...", callback=self._choose_record_path)
        dpg.add_button(label="Record", tag=TAG_REC_BUTTON, width=-1, callback=self._toggle_record)
        dpg.add_text("", tag=TAG_REC_STATUS, wrap=340)
        dpg.add_button(
            label="Recover the unfinished recording",
            tag=TAG_RECOVER,
            width=-1,
            show=False,
            callback=lambda: a.recover(),
        )
        dpg.add_text(
            "Records the sweeps of Live and finished scans to a compressed .ocrec file.",
            color=theme.MUTED_COLOR,
            wrap=340,
        )

        dpg.add_separator()
        dpg.add_text("Replay")
        dpg.add_button(
            label="Open recording...", tag=TAG_REPLAY_OPEN, width=-1, callback=self._open_replay
        )
        dpg.add_button(
            label="Open unfinished recording...",
            tag=TAG_REPLAY_UNFINISHED,
            width=-1,
            callback=self._open_unfinished,
        )
        dpg.add_text("", tag=TAG_REPLAY_INFO, wrap=340)
        with dpg.group(horizontal=True):
            dpg.add_text("Speed")
            dpg.add_combo(
                list(SPEEDS),
                tag=TAG_REPLAY_SPEED,
                default_value="1x",
                width=80,
                callback=lambda _s, label: a.set_replay_speed(str(label)),
            )
            dpg.add_button(
                label="Play",
                tag=TAG_REPLAY_PLAY,
                width=80,
                callback=lambda: a.toggle_replay_pause(),
            )
            dpg.add_button(label="Restart", tag=TAG_REPLAY_RESTART, callback=a.restart_replay)
        dpg.add_progress_bar(tag=TAG_REPLAY_POSITION, width=-1)
        dpg.add_text(
            "A replay is shown in Live mode (Start / Stop also play and pause it). "
            "Scan mode and span changes need a real device.",
            color=theme.MUTED_COLOR,
            wrap=340,
        )

        dpg.add_separator()
        dpg.add_text("Long-run logger")
        dpg.add_checkbox(label="Log max hold to CSV", tag=TAG_LOG_ENABLE, callback=self._on_enable)
        with dpg.group(horizontal=True):
            dpg.add_input_text(tag=TAG_LOG_PATH, default_value=default_log_name(), width=-90)
            dpg.add_button(label="Choose...", callback=self._choose_log_path)
        dpg.add_input_double(
            tag=TAG_LOG_INTERVAL,
            label="Interval (s)",
            default_value=DEFAULT_INTERVAL_S,
            min_value=MIN_INTERVAL_S,
            min_clamped=True,
            step=0,
            format="%.0f",
            on_enter=True,
            width=110,
            callback=self._on_interval,
        )
        with dpg.group(horizontal=True):
            dpg.add_checkbox(
                label="Own alert threshold", tag=TAG_LOG_OWN, callback=self._on_threshold
            )
            dpg.add_input_double(
                tag=TAG_LOG_THRESHOLD,
                default_value=-80.0,
                step=0,
                format="%.1f",
                on_enter=True,
                width=80,
                callback=self._on_threshold,
            )
            dpg.add_text("dBm")
        dpg.add_text("", tag="logger.threshold_note", wrap=340, color=theme.MUTED_COLOR)
        dpg.add_text("Ranges (MHz)")
        for slot in range(MAX_RANGES):
            with dpg.group(horizontal=True, tag=f"logger.range.{slot}", show=False):
                dpg.add_text("", tag=f"logger.range.{slot}.text")
                dpg.add_button(
                    label="Remove",
                    tag=f"logger.range.{slot}.remove",
                    callback=lambda _s, _a, slot=slot: a.remove_logger_range(slot),
                )
        fmt = {"format": "%.3f", "step": 0, "width": 90}
        with dpg.group(horizontal=True):
            dpg.add_input_double(tag=TAG_LOG_NEW_START, **fmt)
            dpg.add_input_double(tag=TAG_LOG_NEW_STOP, **fmt)
            dpg.add_button(label="Add", tag="logger.add", callback=self._add_range)
        dpg.add_button(
            label="Add current view range",
            tag="logger.add_view",
            callback=lambda: a.add_view_range(),
        )
        dpg.add_text("", tag=TAG_LOG_STATUS, wrap=340)
        dpg.add_text("", tag=TAG_LOG_ALERT, color=theme.ERROR_COLOR, wrap=340)
        dpg.add_button(
            label="Clear alert", tag="logger.clear_alert", callback=lambda: a.clear_alert()
        )
        dpg.add_text(
            "Writes one row per range every interval (max level and its frequency since the last "
            "row) while Live or a scan runs. With no ranges the current view range is used. "
            "Alerts use the threshold line unless an own threshold is set. During a replay the "
            "logger works on the recorded time (rows and alerts carry the recording's timestamps).",
            color=theme.MUTED_COLOR,
            wrap=340,
        )

    # --- per frame ---

    def update(self, state: AppState) -> None:
        a = self._a
        rec = a.recording_status()
        self._set(
            TAG_REC_STATUS,
            f"Recording to {rec.path.name}: {clock_text(rec.elapsed_s)}, {rec.sweeps} sweeps"
            if rec.active and rec.path
            else "Finishing the recording file..."
            if rec.finishing or a.finishing
            else "Not recording",
        )
        self._cfg(
            TAG_REC_BUTTON,
            label="Stop recording" if rec.active else "Record",
            enabled=rec.active or (state.connection == "connected" and not a.finishing),
        )
        self._cfg(TAG_RECOVER, show=a.pending_recovery is not None, enabled=not a.finishing)
        self._cfg(TAG_REC_PATH, enabled=not rec.active)

        replay = a.replay_status()
        for tag in (TAG_REPLAY_OPEN, TAG_REPLAY_UNFINISHED):
            self._cfg(tag, enabled=state.connection == "disconnected")
        if replay is None:
            self._set(TAG_REPLAY_INFO, "No recording open")
            self._cfg(TAG_REPLAY_PLAY, label="Play", enabled=False)
            self._cfg(TAG_REPLAY_RESTART, enabled=False)
            self._cfg(TAG_REPLAY_SPEED, enabled=False)
            self._value(TAG_REPLAY_POSITION, 0.0)
            self._cfg(TAG_REPLAY_POSITION, overlay="")
        else:
            running = state.running and not replay.paused
            status = "ended" if replay.ended else "playing" if running else "paused"
            self._set(TAG_REPLAY_INFO, f"{replay.name}: {replay.total_sweeps} sweeps, {status}")
            self._cfg(TAG_REPLAY_PLAY, label="Pause" if running else "Play", enabled=True)
            self._cfg(TAG_REPLAY_RESTART, enabled=True)
            self._cfg(TAG_REPLAY_SPEED, enabled=True)
            if dpg.get_value(TAG_REPLAY_SPEED) != speed_label(replay.speed):
                dpg.set_value(TAG_REPLAY_SPEED, speed_label(replay.speed))
            self._value(TAG_REPLAY_POSITION, replay.position)
            self._cfg(TAG_REPLAY_POSITION, overlay=f"{replay.position * 100:.0f} %")

        logger = a.logger_status()
        if dpg.get_value(TAG_LOG_ENABLE) != logger.enabled:
            dpg.set_value(TAG_LOG_ENABLE, logger.enabled)
        for tag in (
            TAG_LOG_PATH,
            "logger.add",
            "logger.add_view",
            TAG_LOG_NEW_START,
            TAG_LOG_NEW_STOP,
        ):
            self._cfg(tag, enabled=not logger.enabled)
        self._set(
            TAG_LOG_STATUS,
            f"Logging to {logger.path.name}: next row in "
            f"{clock_text(logger.next_row_in_s or 0.0)}, {logger.rows_written} rows, "
            f"{logger.alerts} alerts"
            if logger.enabled and logger.path
            else "Logger off",
        )
        self._set(TAG_LOG_ALERT, state.logger_alert or "")
        own = a.logger_threshold_dbm
        threshold = a.effective_threshold()
        self._set(
            "logger.threshold_note",
            "No alert threshold: tick the box or show the threshold line in the Markers tab"
            if threshold is None
            else f"Alerts above {threshold:.1f} dBm"
            + ("" if own is not None else " (the threshold line)"),
        )
        if state.ui_version != self._ranges_version:
            self._ranges_version = state.ui_version
            if not dpg.is_item_active(TAG_LOG_INTERVAL) and not math.isclose(
                dpg.get_value(TAG_LOG_INTERVAL), a.logger_interval_s
            ):
                dpg.set_value(TAG_LOG_INTERVAL, a.logger_interval_s)
            for slot in range(MAX_RANGES):
                shown = slot < len(a.logger_ranges)
                self._cfg(f"logger.range.{slot}", show=shown)
                if shown:
                    dpg.set_value(f"logger.range.{slot}.text", range_text(a.logger_ranges[slot]))
                    self._cfg(f"logger.range.{slot}.remove", enabled=not logger.enabled)


__all__ = ["RecordPanel", "clock_text", "range_text"]
