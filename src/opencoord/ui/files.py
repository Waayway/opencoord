"""Session and export orchestration for the UI (no Dear PyGui).

:class:`FileActions` turns the controller state into a :class:`~opencoord.core.session.Session`
and back, runs the exporters and importers and reports every outcome in the status line. Failures
(unreadable file, bad format, no data) never raise: they become a status message and a ``False``
result.

Opening a session while a device is connected never touches the device: acquisition is stopped,
the saved range / mode / resolution become the *selected* settings (the next Start tunes to them),
and the saved live / max / avg / min traces are displayed until fresh sweeps replace them. The
saved scan trace is shown as the scan trace; references, markers, zones and threshold are replaced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt

from opencoord import __version__
from opencoord.core import session as session_io
from opencoord.core.analysis import analyze
from opencoord.core.session import DeviceInfo, Session, SessionError, SessionSettings
from opencoord.core.types import Trace
from opencoord.device.scanner import Resolution
from opencoord.io import export_scan, importers
from opencoord.ui.controller import MAX_REFERENCES, Controller

log = logging.getLogger(__name__)

SESSION_SUFFIX: Final = session_io.SESSION_SUFFIX
#: Trace keys restored into the live trace set (the rest are the scan trace and references).
_HELD_KEYS: Final = ("live", "max", "avg", "min")


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    suffix: str
    #: The export needs a trace (``False`` for the plot image).
    needs_trace: bool = True


EXPORT_FORMATS: Final = (
    ExportFormat("generic", "CSV (frequency_mhz, level_dbm)", ".csv"),
    ExportFormat("wwb", "Shure Wireless Workbench CSV", ".csv"),
    ExportFormat("wsm", "Sennheiser WSM CSV", ".csv"),
    ExportFormat("carriers", "Detected carriers CSV", ".csv"),
    ExportFormat("png", "Plot image (PNG)", ".png", needs_trace=False),
)


def export_format(key: str) -> ExportFormat:
    return next(f for f in EXPORT_FORMATS if f.key == key)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def with_suffix(path: Path, suffix: str) -> Path:
    """``path`` with ``suffix`` appended if it has none (dialogs return what was typed)."""
    return path if path.suffix else path.with_name(path.name + suffix)


class FileActions:
    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        #: The session file in use (``None`` until saved or opened).
        self.path: Path | None = None
        self._created: str | None = None

    # --- helpers -----------------------------------------------------------------------------

    def _say(self, message: str) -> None:
        st = self.controller.state
        st.message = message
        st.ui_version += 1

    @property
    def title(self) -> str:
        name = self.path.name if self.path else "untitled"
        return f"{name} - OpenCoord {__version__}"

    def trace_choices(self) -> list[tuple[str, str]]:
        """``(key, label)`` of every trace that has data, in plot order."""
        return [(k, t.label) for k, t in self.controller.state.trace_map().items() if t is not None]

    # --- sessions ----------------------------------------------------------------------------

    def build_session(self) -> Session:
        st = self.controller.state
        now = _now()
        device = None
        if st.model is not None:
            name = st.capabilities.main_name if st.capabilities else ""
            device = DeviceInfo(name, st.model.main_code, st.model.firmware)
        return Session(
            settings=SessionSettings(
                start_hz=st.start_hz,
                stop_hz=st.stop_hz,
                mode=st.mode,
                preset=st.preset,
                resolution=st.resolution.value,
                threshold_dbm=st.threshold_dbm,
                overlay_enabled=st.overlay_enabled,
                channel_plan=st.channel_plan.name if st.channel_plan else None,
            ),
            traces={k: t for k, t in st.trace_map().items() if t is not None},
            markers=list(st.markers),
            exclusion_zones=list(st.exclusion_zones),
            device=device,
            created=self._created or now,
            modified=now,
            opencoord_version=__version__,
        )

    def save(self, path: Path | None = None) -> bool:
        """Save to ``path`` (default: the current file); ``False`` without a path or on failure."""
        target = path or self.path
        if target is None:
            return False
        target = with_suffix(target, SESSION_SUFFIX)
        session = self.build_session()
        try:
            session_io.save(target, session)
        except OSError as exc:
            self._say(f"Cannot save {target.name}: {exc.strerror or exc}")
            return False
        self.path, self._created = target, session.created
        self._say(f"Saved {target}")
        return True

    def open(self, path: Path) -> bool:
        st = self.controller.state
        if st.stopping:
            self._say("Wait for the scan to stop before opening a session")
            return False
        try:
            session = session_io.load(path)
        except SessionError as exc:
            self._say(str(exc))
            return False
        self.apply_session(session)
        self.path, self._created = path, session.created or None
        self._say(f"Opened {path.name}")
        return True

    def apply_session(self, session: Session) -> None:
        c = self.controller
        st = c.state
        if st.running:
            c.stop()
        s = session.settings
        c.set_mode("scan" if s.mode == "scan" else "live")
        try:
            c.set_resolution(Resolution(s.resolution))
        except ValueError:
            log.warning("unknown resolution %r in the session", s.resolution)
        c.set_range(s.start_hz, s.stop_hz, preset=s.preset)
        st.threshold_dbm = s.threshold_dbm
        st.overlay_enabled = s.overlay_enabled
        if s.channel_plan and (st.channel_plan is None or st.channel_plan.name != s.channel_plan):
            c.set_channel_plan(s.channel_plan)
        st.markers = list(session.markers)
        st.selected_marker = st.delta_reference = None
        st.exclusion_zones = list(session.exclusion_zones)

        st.traces.reset()
        held = {k: _read_only(session.traces[k]) for k in _HELD_KEYS if k in session.traces}
        st.traces.live = held.get("live")
        st.traces.max_hold = held.get("max")
        st.traces.average = held.get("avg")
        st.traces.min_hold = held.get("min")
        st.scan_partial = _read_only(session.traces["scan"]) if "scan" in session.traces else None
        refs = {k: _read_only(t) for k, t in session.traces.items() if _is_reference(k)}
        st.references = dict(sorted(refs.items()))
        st.hidden_traces.clear()
        st.waterfall.clear()
        st.trace_version += 1
        st.ui_version += 1

    # --- exports and imports -----------------------------------------------------------------

    def export(
        self,
        fmt_key: str,
        trace_key: str,
        path: Path,
        rgba: npt.NDArray[np.uint8] | None = None,
    ) -> bool:
        fmt = export_format(fmt_key)
        path = with_suffix(path, fmt.suffix)
        try:
            if fmt.key == "png":
                if rgba is None:
                    raise ValueError("No image was captured")
                export_scan.write_bytes(path, export_scan.png_bytes(rgba))
            else:
                trace = self._trace(trace_key)
                export_scan.write_text(path, self._text(fmt.key, trace))
        except (ValueError, OSError) as exc:
            self._say(f"Export failed: {getattr(exc, 'strerror', None) or exc}")
            return False
        self._say(f"Exported {path}")
        return True

    def _trace(self, key: str) -> Trace:
        trace = self.controller.state.trace_map().get(key)
        if trace is None:
            raise ValueError("The chosen trace has no data yet")
        return trace

    def _text(self, fmt_key: str, trace: Trace) -> str:
        if fmt_key == "generic":
            return export_scan.generic_csv(trace)
        if fmt_key == "wwb":
            return export_scan.wwb_csv(trace)
        if fmt_key == "wsm":
            return export_scan.wsm_csv(trace)
        st = self.controller.state
        analysis = analyze(trace, st.channel_plan, st.threshold_dbm, max_rows=None)
        if analysis is None:
            raise ValueError("The chosen trace has no data yet")
        return export_scan.carriers_csv([(r.carrier, r.channel) for r in analysis.carriers])

    def import_reference(self, path: Path) -> bool:
        """Import a scan file (any supported CSV) as a new reference trace."""
        st = self.controller.state
        if len(st.references) >= MAX_REFERENCES:
            self._say(f"At most {MAX_REFERENCES} reference traces; remove one first")
            return False
        try:
            trace = importers.import_file(path)
        except (ValueError, OSError) as exc:
            self._say(f"Cannot import {path.name}: {getattr(exc, 'strerror', None) or exc}")
            return False
        n = next(i for i in range(1, MAX_REFERENCES + 1) if f"ref{i}" not in st.references)
        trace = Trace(trace.freqs_hz, trace.dbm, f"Ref {n}: {trace.label}")
        st.references = dict(sorted({**st.references, f"ref{n}": _read_only(trace)}.items()))
        st.trace_version += 1
        st.ui_version += 1
        self._say(f"Imported {path.name} as reference {n} ({len(trace.freqs_hz)} points)")
        return True


def _is_reference(key: str) -> bool:
    return key.startswith("ref") and key[3:].isdigit() and 1 <= int(key[3:]) <= MAX_REFERENCES


def _read_only(trace: Trace) -> Trace:
    trace.dbm.setflags(write=False)
    trace.freqs_hz.setflags(write=False)
    return trace
