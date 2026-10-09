"""Coordination orchestration for the UI (no Dear PyGui).

:class:`CoordinationActions` owns the Coordination tab: the setup (:class:`~opencoord.ui.
coordination_model.CoordinationModel`), the last solved plan, the last check report, the
background job and the plan exports. The panel only renders this object and calls its methods.

* **Coordinate** builds a :class:`~opencoord.coord.solver.CoordinationRequest` from the setup and
  the app context (loaded profiles + built-in templates, loaded spacing presets, the main trace =
  max hold, else scan, else live, unless "use scan" is off, the exclusion zones and the channel
  plan), then runs ``solve`` on a worker thread. The result comes back through a queue drained
  by ``Controller.on_tick`` on the UI thread; nothing here touches ``AppState`` off that thread.
  **Cancel** abandons the job: its result is dropped when it arrives (the solver stops by itself
  at its time budget).
* **Check** validates the frequencies typed in the device rows with ``check`` (same worker).
* Exports (CSV, TXT, HTML with an image of the plot) go through :mod:`opencoord.io.export_plan`.
* Sessions: :meth:`session_parts` / :meth:`apply_session` (called by ``FileActions``).

Failures never raise: they become a message (also in the status bar) and a ``False`` result.
``version`` changes whenever anything shown here changes (the model has its own ``revision``).
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from opencoord import __version__
from opencoord.coord.profiles import DeviceProfile, builtin_templates
from opencoord.coord.solver import (
    Assignment,
    CheckReport,
    CoordinationRequest,
    Plan,
    check,
    solve,
)
from opencoord.coord.spacing import SpacingRules, builtin_presets
from opencoord.core.types import Trace
from opencoord.io import export_plan
from opencoord.io.png import encode_png
from opencoord.ui.controller import Controller
from opencoord.ui.coordination_model import (
    DEFAULT_LOCK_PRESET,
    CoordinationModel,
    CoordinationResult,
    LockRow,
    build_request,
    check_input,
    result_from_dict,
    result_to_dict,
)
from opencoord.ui.profiles_actions import ProfilesActions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanExport:
    key: str
    label: str
    suffix: str


PLAN_EXPORTS: Final = (
    PlanExport("csv", "CSV", ".csv"),
    PlanExport("txt", "Text", ".txt"),
    PlanExport("html", "Printable HTML", ".html"),
)


def plan_export(key: str) -> PlanExport:
    return next(f for f in PLAN_EXPORTS if f.key == key)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _with_suffix(path: Path, suffix: str) -> Path:
    return path if path.suffix else path.with_name(path.name + suffix)


@dataclass(frozen=True)
class CheckOutcome:
    """A check of hand-entered frequencies: what was checked and what was found."""

    assignments: tuple[Assignment, ...]
    report: CheckReport


@dataclass(frozen=True)
class _Job:
    id: int
    kind: str  # "solve" | "check"
    started: float
    budget_s: float
    #: Only for "solve": what the result records next to the plan.
    locked: tuple[LockRow, ...] = ()
    scan_label: str | None = None
    solve_key: str = ""
    assignments: tuple[Assignment, ...] = ()
    #: Set when the job is abandoned: the solver's clock then jumps past its deadline.
    stop: threading.Event = field(default_factory=threading.Event, compare=False)


def _stoppable(clock: Callable[[], float], stop: threading.Event) -> Callable[[], float]:
    """``clock`` that jumps far ahead once ``stop`` is set, so the solver's time budget ends
    the search at its next node (``solve`` has no other way to be interrupted)."""

    def tick() -> float:
        return clock() + (1e12 if stop.is_set() else 0.0)

    return tick


def _trace_digest(trace: Trace) -> str:
    h = hashlib.blake2b(digest_size=12)
    h.update(trace.label.encode())
    h.update(np.ascontiguousarray(trace.freqs_hz).tobytes())
    h.update(np.ascontiguousarray(trace.dbm).tobytes())
    return h.hexdigest()


@dataclass(frozen=True)
class _Done:
    job_id: int
    value: Plan | CheckReport | BaseException


class CoordinationActions:
    def __init__(
        self,
        controller: Controller,
        profiles: ProfilesActions,
        say: Callable[[str], None] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], str] = _now,
        solver: Callable[[CoordinationRequest], Plan] = solve,
        checker: Callable[[list[Assignment], CoordinationRequest], CheckReport] = check,
    ) -> None:
        self.controller = controller
        self.profiles = profiles
        self._say = say
        self._clock = clock
        self._timestamp = timestamp
        self._solver = solver
        self._checker = checker
        self.model = CoordinationModel()
        self.result: CoordinationResult | None = None
        #: ``model.solve_key()`` of the setup the result was made from.
        self.result_key: str | None = None
        #: ``(trace_version, digest)`` of the main trace (hashing it once per new data).
        self._scan_digest: tuple[int, str | None] | None = None
        self.check_outcome: CheckOutcome | None = None
        self.show_on_spectrum = True
        self.message = ""
        self.message_is_error = False
        self.version = 0
        self._job: _Job | None = None
        self._ids = itertools.count(1)
        self._done: queue.Queue[_Done] = queue.Queue()
        controller.on_tick.append(self._on_tick)
        controller.on_shutdown.append(self._on_shutdown)

    # --- plumbing ------------------------------------------------------------------------------

    def _bump(self) -> None:
        self.version += 1

    def say(self, message: str, *, error: bool = False) -> None:
        self.message = message
        self.message_is_error = error
        self._bump()
        if self._say is not None:
            self._say(message)

    # --- context -------------------------------------------------------------------------------

    def profile_map(self) -> dict[str, DeviceProfile]:
        """Profiles a device row can use: the built-in templates, then the user's (which win)."""
        out = {p.name: p for p in builtin_templates().values()}
        out.update(self.profiles.profiles)
        return out

    def profile_names(self) -> list[str]:
        """The user's profiles (alphabetical), then the templates they do not shadow."""
        own = list(self.profiles.profiles)
        templates = sorted(
            (p.name for p in builtin_templates().values() if p.name not in self.profiles.profiles),
            key=str.casefold,
        )
        return own + templates

    def presets(self) -> dict[str, SpacingRules]:
        loaded = {name: p.rules for name, p in self.profiles.presets.items()}
        return loaded or {name: p.rules for name, p in builtin_presets().items()}

    def preset_names(self) -> list[str]:
        return sorted(self.presets())

    def default_lock_preset(self) -> str:
        names = self.preset_names()
        return DEFAULT_LOCK_PRESET if DEFAULT_LOCK_PRESET in names or not names else names[0]

    def scan_trace(self) -> Trace | None:
        """The trace the solver would use (``None`` with "use scan" off or no data)."""
        if not self.model.options.use_scan:
            return None
        found = self.controller.resolve_trace("max")
        if found is None:
            return None
        t = found[1]
        # A copy: the live max hold may be updated in place while the worker reads it.
        return Trace(np.array(t.freqs_hz, copy=True), np.array(t.dbm, copy=True), t.label)

    def _context(self, scan: Trace | None) -> dict[str, Any]:
        st = self.controller.state
        return {
            "profiles": self.profile_map(),
            "presets": self.presets(),
            "scan": scan,
            "zones": tuple(st.exclusion_zones),
            "channel_plan": st.channel_plan,
        }

    # --- jobs ----------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._job is not None

    @property
    def running_kind(self) -> str | None:
        return None if self._job is None else self._job.kind

    def progress_text(self) -> str:
        job = self._job
        if job is None:
            return ""
        elapsed = max(self._clock() - job.started, 0.0)
        if job.kind == "check":
            return f"Checking... ({elapsed:.1f} s)"
        return f"Coordinating... ({elapsed:.1f} s of up to {job.budget_s:g} s)"

    def _start(self, job: _Job, work: Callable[[], Plan | CheckReport]) -> None:
        self._job = job

        def run() -> None:
            if job.stop.is_set():
                return  # abandoned before it started
            try:
                value: Plan | CheckReport | BaseException = work()
            except BaseException as exc:  # reported on the UI thread
                value = exc
            self._done.put(_Done(job.id, value))

        threading.Thread(target=run, name=f"opencoord-{job.kind}", daemon=True).start()
        self._bump()

    def coordinate(self) -> bool:
        """Start a coordination run in the background; ``False`` if it could not start."""
        if self._job is not None:
            self.say("A coordination is already running", error=True)
            return False
        scan = self.scan_trace()
        try:
            request = build_request(self.model, **self._context(scan))
        except ValueError as exc:
            self.say(f"Cannot coordinate: {exc}", error=True)
            return False
        stop = threading.Event()
        request = replace(request, clock=_stoppable(request.clock, stop))
        job = _Job(
            next(self._ids),
            "solve",
            self._clock(),
            request.time_budget_s,
            locked=tuple(self.model.locks),
            scan_label=None if scan is None else scan.label,
            solve_key=self.solve_key(),
            stop=stop,
        )
        self._start(job, lambda: self._solver(request))
        self.say("Coordinating...")
        return True

    def check(self) -> bool:
        """Check the frequencies typed in the device rows in the background."""
        if self._job is not None:
            self.say("Wait for the running job to finish (or cancel it)", error=True)
            return False
        try:
            request, assignments = check_input(self.model, **self._context(self.scan_trace()))
        except ValueError as exc:
            self.say(f"Cannot check: {exc}", error=True)
            return False
        job = _Job(
            next(self._ids),
            "check",
            self._clock(),
            0.0,
            assignments=tuple(assignments),
        )
        self._start(job, lambda: self._checker(assignments, request))
        self.say("Checking...")
        return True

    def cancel(self) -> None:
        """Abandon the running job (its result is ignored when it arrives)."""
        if self._job is None:
            return
        kind = self._job.kind
        self._drop_job()
        self.say("Coordination cancelled" if kind == "solve" else "Check cancelled")

    def _drop_job(self) -> None:
        """Forget the running job and make its solver stop at its next node."""
        if self._job is not None:
            self._job.stop.set()
            self._job = None

    def _on_shutdown(self) -> None:
        self._drop_job()

    def _on_tick(self, _now: float) -> None:
        while True:
            try:
                done = self._done.get_nowait()
            except queue.Empty:
                return
            job = self._job
            if job is None or done.job_id != job.id:
                continue  # cancelled or superseded
            self._job = None
            self._finish(job, done.value)

    def _finish(self, job: _Job, value: Plan | CheckReport | BaseException) -> None:
        if isinstance(value, BaseException):
            log.error("coordination job failed", exc_info=value)
            what = "Coordination" if job.kind == "solve" else "Check"
            self.say(f"{what} failed: {value}", error=True)
            return
        if isinstance(value, CheckReport):
            self.check_outcome = CheckOutcome(job.assignments, value)
            n = len(value.violations)
            self.say(
                f"Check: no violations among {len(job.assignments)} devices"
                if n == 0
                else f"Check: {n} violation{'s' if n != 1 else ''} found",
                error=n > 0,
            )
            return
        self.result = CoordinationResult(
            value, job.locked, job.scan_label, self._timestamp(), job.solve_key
        )
        self.result_key = job.solve_key
        s = value.stats
        total = export_plan.device_count(value)
        self.say(
            f"Coordinated {len(value.assignments)} of {total} devices in {s.elapsed_s:.2f} s"
            + ("" if s.complete else " (partial)"),
            error=not s.complete,
        )

    # --- results -------------------------------------------------------------------------------

    def _scan_key(self) -> str | None:
        """Digest of the trace a run would use now (``None``: no scan / "use scan" off)."""
        if not self.model.options.use_scan:
            return None
        version = self.controller.state.trace_version
        if self._scan_digest is None or self._scan_digest[0] != version:
            found = self.controller.resolve_trace("max")
            self._scan_digest = (version, None if found is None else _trace_digest(found[1]))
        return self._scan_digest[1]

    def solve_key(self) -> str:
        """Fingerprint of everything a run depends on: the setup (check texts excluded), the
        profiles and presets it uses, zones, channel plan and the scan data."""
        profiles, presets = self.profile_map(), self.presets()
        used = sorted({r.profile for r in self.model.rows if r.quantity > 0})
        context = [
            [
                (n, repr(p), repr(presets.get(p.spacing_preset)))
                for n in used
                if (p := profiles.get(n))
            ],
            [repr(presets.get(lk.preset)) for lk in self.model.locks],
            repr(tuple(self.controller.state.exclusion_zones)),
            getattr(self.controller.state.channel_plan, "name", None),
            self._scan_key(),
        ]
        h = hashlib.blake2b(digest_size=16)
        h.update(self.model.solve_key().encode())
        h.update(repr(context).encode())
        return h.hexdigest()

    @property
    def stale(self) -> bool:
        """The setup, profiles, zones, plan or scan changed since the shown plan was made
        (check-mode texts do not count)."""
        return self.result is not None and self.result_key != self.solve_key()

    def clear_result(self) -> None:
        self.result = None
        self.result_key = None
        self._bump()

    def clear_check(self) -> None:
        self.check_outcome = None
        self._bump()

    def fill_check_from_result(self) -> bool:
        if self.result is None:
            self.say("Coordinate first, then edit the result", error=True)
            return False
        self.model.fill_check(self.result.plan)
        self.say("Copied the plan into the check boxes; edit them and press Check")
        return True

    def set_show_on_spectrum(self, on: bool) -> None:
        self.show_on_spectrum = bool(on)
        self._bump()

    def spectrum_lines(self) -> tuple[list[tuple[int, str]], list[int]]:
        """``([(freq_hz, label)] assigned, [freq_hz] backups)`` to draw (empty when hidden)."""
        if not self.show_on_spectrum or self.result is None:
            return [], []
        plan = self.result.plan
        assigned = [(a.freq_hz, a.label) for a in plan.assignments]
        backups = [f for freqs in plan.backups.values() for f in freqs]
        return assigned, backups

    # --- exports -------------------------------------------------------------------------------

    def document(self) -> export_plan.PlanDocument | None:
        r = self.result
        if r is None:
            return None
        return export_plan.PlanDocument(
            plan=r.plan,
            locked=tuple((lk.freq_hz, lk.label) for lk in r.locked),
            generated=self._timestamp(),
            version=__version__,
            scan_label=r.scan_label,
        )

    def export(self, key: str, path: Path, rgba: npt.NDArray[np.uint8] | None = None) -> bool:
        """Write the plan as ``key`` (csv / txt / html; html embeds ``rgba`` as a PNG)."""
        fmt = plan_export(key)
        doc = self.document()
        if doc is None:
            self.say("Nothing to export: coordinate first", error=True)
            return False
        path = _with_suffix(path, fmt.suffix)
        try:
            if fmt.key == "csv":
                text = export_plan.plan_csv(doc)
            elif fmt.key == "txt":
                text = export_plan.plan_txt(doc)
            else:
                text = export_plan.plan_html(doc, None if rgba is None else encode_png(rgba))
            export_plan.write_text(path, text)
        except (ValueError, OSError) as exc:
            self.say(f"Export failed: {getattr(exc, 'strerror', None) or exc}", error=True)
            return False
        self.say(f"Exported the plan to {path}")
        return True

    # --- sessions ------------------------------------------------------------------------------

    def session_parts(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """``(setup, plan)`` for the session file."""
        plan = None if self.result is None else result_to_dict(self.result)
        return self.model.to_dict(), plan

    def apply_session(
        self, setup: Mapping[str, Any] | None, plan: Mapping[str, Any] | None
    ) -> None:
        """Replace the setup and plan with a session's (bad data becomes a message)."""
        self._drop_job()
        problems = []
        try:
            model = CoordinationModel.from_dict(setup or {})
        except (ValueError, TypeError, OverflowError) as exc:
            problems.append(f"the coordination setup ({exc})")
            model = CoordinationModel()
        # Keep counting upwards so views notice the swap.
        model.revision = self.model.revision + 1
        model.structure_version = self.model.structure_version + 1
        self.model = model
        self.result = None
        if plan is not None:
            try:
                self.result = result_from_dict(plan)
            except (ValueError, TypeError, OverflowError) as exc:
                problems.append(f"the frequency plan ({exc})")
        # The key saved with the plan: a plan made from other data reopens as stale.
        self.result_key = self.result.solve_key if self.result is not None else None
        self.check_outcome = None
        self._bump()
        if problems:
            self.say(f"Ignored unreadable parts of the session: {'; '.join(problems)}", error=True)


__all__ = [
    "PLAN_EXPORTS",
    "CheckOutcome",
    "CoordinationActions",
    "PlanExport",
    "plan_export",
]
