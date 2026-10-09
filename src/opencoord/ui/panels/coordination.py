"""Coordination tab: devices, locked carriers, options, Coordinate, results, exports, check.

All behaviour is in :class:`opencoord.ui.coordination_actions.CoordinationActions` and its
:class:`~opencoord.ui.coordination_model.CoordinationModel`; this panel renders them and forwards
edits. Widget callbacks (UI thread) only change the model or call an action. ``update()`` rebuilds
the row widgets when the model's structure changed (rows added / removed, a session opened, the
check boxes filled from a plan) and the result / check tables when a new outcome arrived;
otherwise it only rewrites texts that changed, so typing is never interrupted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import dearpygui.dearpygui as dpg
import numpy as np
import numpy.typing as npt

from opencoord.coord.solver import (
    RULE_TEXT,
    Assignment,
    Plan,
    Violation,
    describe_unassigned,
)
from opencoord.io import export_plan
from opencoord.ui import theme
from opencoord.ui.coordination_actions import (
    PLAN_EXPORTS,
    CheckOutcome,
    CoordinationActions,
    plan_export,
)
from opencoord.ui.coordination_model import (
    BACKUPS_LIMITS,
    GUARD_KHZ_LIMITS,
    SCALE_LIMITS,
    THRESHOLD_DB_LIMITS,
    TIME_BUDGET_S_LIMITS,
    CoordinationModel,
    CoordinationResult,
)
from opencoord.ui.profile_editor import SPACING_FIELDS, SPACING_LABELS, format_mhz

#: ``ask(title, extensions, action, default_name=...)``: a file dialog (see ``FileUI.ask``).
AskFile = Callable[..., None]
#: ``capture(done)``: grab the plot image a few frames later (see ``FileUI.capture_plot``).
Capture = Callable[[Callable[[npt.NDArray[np.uint8] | None], None]], None]

WRAP = 340
PROFILE_PLACEHOLDER = "(choose a profile)"

TAG_MESSAGE = "coord.message"
TAG_DEVICES = "coord.devices"
TAG_DEVICE_ADD = "coord.device.add"
TAG_TOTAL = "coord.devices.total"
TAG_LOCK_PASTE = "coord.lock.paste"
TAG_LOCK_LABEL = "coord.lock.label"
TAG_LOCK_PRESET = "coord.lock.preset"
TAG_LOCK_ADD = "coord.lock.add"
TAG_LOCK_ERRORS = "coord.lock.errors"
TAG_LOCKS = "coord.locks"
TAG_USE_SCAN = "coord.use_scan"
TAG_SCAN_INFO = "coord.scan_info"
TAG_THRESHOLD = "coord.threshold"
TAG_GUARD = "coord.guard"
TAG_FORBIDDEN = "coord.allow_forbidden"
TAG_SINGLE_GROUP = "coord.single_group"
TAG_BUDGET = "coord.budget"
TAG_BACKUPS = "coord.backups"
TAG_OVERRIDE = "coord.override"
TAG_SCALE = "coord.override.scale"
TAG_RUN = "coord.run"
TAG_CANCEL = "coord.cancel"
TAG_PROGRESS = "coord.progress"
TAG_STATS = "coord.stats"
TAG_STALE = "coord.stale"
TAG_WARNINGS = "coord.warnings"
TAG_RESULTS = "coord.results"
TAG_UNASSIGNED = "coord.unassigned"
TAG_BACKUP_LIST = "coord.backup_list"
TAG_SHOW = "coord.show"
TAG_EDIT = "coord.edit"
TAG_CLEAR = "coord.clear"
TAG_CHECK_ROWS = "coord.check.rows"
TAG_CHECK_RUN = "coord.check.run"
TAG_CHECK_SUMMARY = "coord.check.summary"
TAG_CHECK_TABLE = "coord.check.table"
TAG_CHECK_WARNINGS = "coord.check.warnings"

#: Tags of rebuilt widgets (their cached texts and settings are dropped on a rebuild).
_DYNAMIC_PREFIXES = ("coord.device.", "coord.lock.row.", "coord.check.row.")


def override_tag(name: str, part: str) -> str:
    return f"coord.override.{name}.{part}"


def export_tag(key: str) -> str:
    return f"coord.export.{key}"


# --- texts (pure) ---------------------------------------------------------------------------------


def stats_text(plan: Plan) -> str:
    """``Complete: ...`` / ``Partial plan: ...`` plus nodes and time."""
    return f"{export_plan.status_line(plan)}. {export_plan.search_line(plan)}"


def _row_warning(a: Assignment, warnings: tuple[str, ...]) -> str:
    """``!`` when a plan warning is about this device (forbidden band)."""
    prefix = f"{a.label} at "
    return "!" if any(w.startswith(prefix) for w in warnings) else ""


def result_rows(plan: Plan) -> list[tuple[str, str, str, str, str, str]]:
    """Cells of the result table: device, MHz, group, scan dBm, nearest IMD kHz, warning."""
    return [
        (
            a.label,
            f"{a.freq_hz / 1e6:.3f}",
            a.group or "-",
            "-" if a.scan_level_dbm is None else f"{a.scan_level_dbm:.1f}",
            "-" if a.nearest_imd_margin_hz is None else f"{a.nearest_imd_margin_hz / 1e3:.0f}",
            _row_warning(a, plan.warnings),
        )
        for a in plan.assignments
    ]


def unassigned_text(plan: Plan) -> str:
    return "\n".join(f"{u.label}: {describe_unassigned(u)}" for u in plan.unassigned)


def backups_text(plan: Plan) -> str:
    lines = [
        f"{name}: {', '.join(f'{f / 1e6:.3f}' for f in freqs) or 'none found'}"
        for name, freqs in plan.backups.items()
    ]
    return "Backups (MHz)\n" + "\n".join(lines) if lines else ""


def violation_cells(v: Violation) -> tuple[str, str, str, str]:
    """``(rule, required kHz, actual kHz, involved)`` for the check table."""
    who = ", ".join(v.sources)
    if v.product_hz is not None:
        who += f" -> {v.victim or '?'} (product {v.product_hz / 1e6:.3f})"
    return (
        RULE_TEXT.get(v.rule, v.rule),
        f"{v.required_hz / 1e3:g}",
        f"{v.actual_hz / 1e3:g}",
        who,
    )


def check_summary(outcome: CheckOutcome) -> str:
    n, k = len(outcome.report.violations), len(outcome.assignments)
    if n == 0:
        return f"No violations among {k} devices"
    return f"{n} violation{'s' if n != 1 else ''} among {k} devices"


class CoordinationPanel:
    def __init__(self, actions: CoordinationActions, ask: AskFile, capture: Capture) -> None:
        self._a = actions
        self._ask = ask
        self._capture = capture
        self._text: dict[str, str] = {}
        self._configured: dict[str, str] = {}
        self._dynamic_inputs: list[str] = []
        self._structure: tuple[int, int, tuple[str, ...], tuple[str, ...]] | None = None
        self._seen: tuple[object, ...] = ()
        self._result: CoordinationResult | None = None
        self._outcome: CheckOutcome | None = None
        self._static_inputs = [
            TAG_LOCK_PASTE,
            TAG_LOCK_LABEL,
            TAG_THRESHOLD,
            TAG_GUARD,
            TAG_BUDGET,
            TAG_BACKUPS,
            TAG_SCALE,
            *(override_tag(f, "value") for f in SPACING_FIELDS),
        ]

    # --- helpers ---------------------------------------------------------------------------

    @property
    def text_inputs(self) -> list[str]:
        return list(self._static_inputs)

    def is_typing(self) -> bool:
        """One of the rebuilt row fields has the keyboard (shortcuts must stay quiet)."""
        return any(dpg.does_item_exist(t) and dpg.is_item_active(t) for t in self._dynamic_inputs)

    def _set(self, tag: str, text: str) -> None:
        if self._text.get(tag) != text:
            self._text[tag] = text
            dpg.set_value(tag, text)

    def _cfg(self, tag: str, **kwargs: object) -> None:
        key = repr(sorted(kwargs.items()))
        if self._configured.get(tag) != key:
            self._configured[tag] = key
            dpg.configure_item(tag, **kwargs)

    def _value(self, tag: str, value: object) -> None:
        """Set a widget value unless the user is editing it or it already shows ``value``."""
        if dpg.get_value(tag) != value and not dpg.is_item_active(tag):
            dpg.set_value(tag, value)

    def _dyn(self, tag: str) -> str:
        self._dynamic_inputs.append(tag)
        return tag

    # --- callbacks -------------------------------------------------------------------------

    @property
    def _m(self) -> CoordinationModel:
        """The model (looked up each time: opening a session swaps it)."""
        return self._a.model

    def _on_profile(self, _s: object, name: str, index: int) -> None:
        self._m.set_device_profile(index, "" if name == PROFILE_PLACEHOLDER else name)

    def _on_quantity(self, _s: object, value: int, index: int) -> None:
        self._m.set_quantity(index, int(value))

    def _on_remove_device(self, _s: object, _v: object, index: int) -> None:
        self._m.remove_device(index)

    def _add_device(self) -> None:
        names = self._a.profile_names()
        if self._m.add_device(names[0] if names else "") is None:
            self._a.say("That is the maximum number of device rows", error=True)

    def _add_locks(self) -> None:
        result = self._m.paste_locks(
            str(dpg.get_value(TAG_LOCK_PASTE)),
            str(dpg.get_value(TAG_LOCK_LABEL)),
            str(dpg.get_value(TAG_LOCK_PRESET)) or self._a.default_lock_preset(),
        )
        self._set(TAG_LOCK_ERRORS, "\n".join(result.errors[:4]))
        if result.ok:
            dpg.set_value(TAG_LOCK_PASTE, "")
            dpg.set_value(TAG_LOCK_LABEL, "")

    def _on_lock_label(self, _s: object, value: str, index: int) -> None:
        self._m.set_lock_label(index, value)

    def _on_lock_preset(self, _s: object, value: str, index: int) -> None:
        self._m.set_lock_preset(index, value)

    def _on_remove_lock(self, _s: object, _v: object, index: int) -> None:
        self._m.remove_lock(index)

    def _on_check_text(self, _s: object, value: str, index: int) -> None:
        self._m.set_check_text(index, value)

    def _on_override_check(self, _s: object, checked: bool, name: str) -> None:
        value = float(dpg.get_value(override_tag(name, "value"))) if checked else None
        self._m.set_override_value(name, value)

    def _on_override_value(self, _s: object, value: float, name: str) -> None:
        if name in self._m.options.override_khz:
            self._m.set_override_value(name, float(value))

    def _export(self, _s: object, _v: object, key: str) -> None:
        fmt = plan_export(key)
        if key == "html":

            def chosen(path: Path) -> None:
                def done(rgba: npt.NDArray[np.uint8] | None) -> None:
                    self._a.export("html", path, rgba)

                self._capture(done)

        else:

            def chosen(path: Path) -> None:
                self._a.export(key, path)

        self._ask(
            f"Export plan as {fmt.label}",
            [fmt.suffix],
            chosen,
            default_name=f"opencoord-plan{fmt.suffix}",
        )

    # --- layout ----------------------------------------------------------------------------

    def build(self) -> None:
        a = self._a
        dpg.add_text("", tag=TAG_MESSAGE, wrap=WRAP)
        dpg.add_text("Devices (profile and quantity)")
        dpg.add_group(tag=TAG_DEVICES)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Add device", tag=TAG_DEVICE_ADD, callback=self._add_device)
            dpg.add_text("", tag=TAG_TOTAL, color=theme.MUTED_COLOR)
        with dpg.collapsing_header(label="Locked carriers", tag="coord.locks.header"):
            dpg.add_text(
                "Transmitters that cannot move (MHz). Paste several at once: one per line or "
                "separated by ; tab, space or comma.",
                color=theme.MUTED_COLOR,
                wrap=WRAP,
            )
            dpg.add_input_text(tag=TAG_LOCK_PASTE, hint="e.g. 606.5; 610.25", width=-1)
            with dpg.group(horizontal=True):
                dpg.add_input_text(tag=TAG_LOCK_LABEL, hint="Label (optional)", width=150)
                dpg.add_combo([], tag=TAG_LOCK_PRESET, width=120)
                dpg.add_button(label="Lock", tag=TAG_LOCK_ADD, callback=self._add_locks)
            dpg.add_text("", tag=TAG_LOCK_ERRORS, color=theme.ERROR_COLOR, wrap=WRAP)
            dpg.add_group(tag=TAG_LOCKS)
            dpg.add_button(label="Clear all", callback=lambda: self._m.clear_locks())
        with dpg.collapsing_header(label="Options", tag="coord.options.header"):
            self._build_options()
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="Coordinate", tag=TAG_RUN, width=110, callback=a.coordinate)
            dpg.add_button(label="Cancel", tag=TAG_CANCEL, callback=a.cancel, show=False)
            dpg.add_text("", tag=TAG_PROGRESS)
        dpg.add_text("", tag=TAG_STATS, wrap=WRAP)
        dpg.add_text("", tag=TAG_STALE, color=theme.WARN_COLOR, wrap=WRAP)
        dpg.add_text("", tag=TAG_WARNINGS, color=theme.ERROR_COLOR, wrap=WRAP)
        with dpg.table(
            tag=TAG_RESULTS,
            header_row=True,
            borders_innerH=True,
            policy=dpg.mvTable_SizingStretchProp,
            show=False,
        ):
            for label in ("Device", "MHz", "Group", "dBm", "IMD kHz", "!"):
                dpg.add_table_column(label=label)
        dpg.add_text("", tag=TAG_UNASSIGNED, color=theme.WARN_COLOR, wrap=WRAP)
        dpg.add_text("", tag=TAG_BACKUP_LIST, wrap=WRAP)
        with dpg.group(horizontal=True):
            dpg.add_checkbox(
                label="Show on spectrum",
                tag=TAG_SHOW,
                default_value=a.show_on_spectrum,
                callback=lambda _s, v: a.set_show_on_spectrum(bool(v)),
            )
            dpg.add_button(label="Edit as check", tag=TAG_EDIT, callback=a.fill_check_from_result)
            dpg.add_button(label="Clear", tag=TAG_CLEAR, callback=a.clear_result)
        with dpg.group(horizontal=True):
            dpg.add_text("Export:")
            for fmt in PLAN_EXPORTS:
                dpg.add_button(
                    label=fmt.label,
                    tag=export_tag(fmt.key),
                    user_data=fmt.key,
                    callback=self._export,
                )
        with dpg.collapsing_header(label="Check a hand-made plan", tag="coord.check.header"):
            dpg.add_text(
                "Type or paste the frequencies (MHz) of each device row, or use 'Edit as "
                "check' on a result, then press Check.",
                color=theme.MUTED_COLOR,
                wrap=WRAP,
            )
            dpg.add_group(tag=TAG_CHECK_ROWS)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Check", tag=TAG_CHECK_RUN, width=110, callback=a.check)
                dpg.add_text("", tag=TAG_CHECK_SUMMARY)
            with dpg.table(
                tag=TAG_CHECK_TABLE,
                header_row=True,
                borders_innerH=True,
                policy=dpg.mvTable_SizingStretchProp,
                show=False,
            ):
                for label in ("Rule", "Needs kHz", "Is kHz", "Involved"):
                    dpg.add_table_column(label=label)
            dpg.add_text("", tag=TAG_CHECK_WARNINGS, color=theme.WARN_COLOR, wrap=WRAP)

    def _build_options(self) -> None:
        m = self._m

        def number(tag: str, label: str, limits: tuple[float, float], setter: str) -> None:
            dpg.add_input_double(
                tag=tag,
                label=label,
                width=90,
                step=0,
                format="%.1f",
                min_value=limits[0],
                max_value=limits[1],
                min_clamped=True,
                max_clamped=True,
                callback=lambda _s, v: getattr(self._m, setter)(float(v)),
            )

        with dpg.group(horizontal=True):
            dpg.add_checkbox(
                label="Use scan",
                tag=TAG_USE_SCAN,
                default_value=m.options.use_scan,
                callback=lambda _s, v: self._m.set_use_scan(bool(v)),
            )
            dpg.add_text("", tag=TAG_SCAN_INFO, color=theme.MUTED_COLOR)
        number(TAG_THRESHOLD, "Occupied above floor (dB)", THRESHOLD_DB_LIMITS, "set_threshold_db")
        number(TAG_GUARD, "Guard band (kHz)", GUARD_KHZ_LIMITS, "set_guard_khz")
        dpg.add_checkbox(
            label="Allow forbidden bands (plan gets a warning)",
            tag=TAG_FORBIDDEN,
            callback=lambda _s, v: self._m.set_allow_forbidden(bool(v)),
        )
        dpg.add_checkbox(
            label="Prefer one group per profile",
            tag=TAG_SINGLE_GROUP,
            default_value=True,
            callback=lambda _s, v: self._m.set_prefer_single_group(bool(v)),
        )
        number(TAG_BUDGET, "Time budget (s)", TIME_BUDGET_S_LIMITS, "set_time_budget_s")
        dpg.add_input_int(
            tag=TAG_BACKUPS,
            label="Backups per profile",
            width=90,
            step=0,
            min_value=BACKUPS_LIMITS[0],
            max_value=BACKUPS_LIMITS[1],
            min_clamped=True,
            max_clamped=True,
            callback=lambda _s, v: self._m.set_backups(int(v)),
        )
        dpg.add_checkbox(
            label="Run override (this run only)",
            tag=TAG_OVERRIDE,
            callback=lambda _s, v: self._m.set_override_enabled(bool(v)),
        )
        with dpg.group(tag="coord.override.fields", indent=16):
            number(TAG_SCALE, "Scale all spacings", SCALE_LIMITS, "set_override_scale")
            dpg.configure_item(TAG_SCALE, format="%.2f")
            for name in SPACING_FIELDS:
                with dpg.group(horizontal=True):
                    dpg.add_checkbox(
                        tag=override_tag(name, "check"),
                        user_data=name,
                        callback=self._on_override_check,
                    )
                    dpg.add_input_double(
                        tag=override_tag(name, "value"),
                        label=f"{SPACING_LABELS[name]} (kHz)",
                        width=80,
                        step=0,
                        format="%.0f",
                        min_value=0.0,
                        min_clamped=True,
                        user_data=name,
                        callback=self._on_override_value,
                    )

    # --- rebuilt rows ----------------------------------------------------------------------

    def _rebuild_rows(self, names: list[str], presets: list[str]) -> None:
        for parent in (TAG_DEVICES, TAG_LOCKS, TAG_CHECK_ROWS):
            dpg.delete_item(parent, children_only=True)
        self._dynamic_inputs = []
        for cache in (self._text, self._configured):
            for tag in [t for t in cache if t.startswith(_DYNAMIC_PREFIXES)]:
                del cache[tag]
        m = self._m
        for i, row in enumerate(m.rows):
            # An empty row offers the placeholder; a profile that is gone stays visible.
            items = names if row.profile in names else [row.profile or PROFILE_PLACEHOLDER, *names]
            with dpg.group(horizontal=True, parent=TAG_DEVICES):
                dpg.add_combo(
                    items,
                    tag=f"coord.device.{i}.profile",
                    default_value=row.profile or PROFILE_PLACEHOLDER,
                    width=210,
                    user_data=i,
                    callback=self._on_profile,
                )
                dpg.add_input_int(
                    tag=self._dyn(f"coord.device.{i}.qty"),
                    default_value=row.quantity,
                    width=80,
                    min_value=0,
                    max_value=200,
                    min_clamped=True,
                    max_clamped=True,
                    user_data=i,
                    callback=self._on_quantity,
                )
                dpg.add_button(
                    label="X",
                    tag=f"coord.device.{i}.remove",
                    user_data=i,
                    callback=self._on_remove_device,
                )
            with dpg.group(horizontal=True, parent=TAG_CHECK_ROWS):
                dpg.add_text("", tag=f"coord.check.row.{i}.name")
                dpg.add_input_text(
                    tag=self._dyn(f"coord.check.row.{i}"),
                    default_value=row.check_text,
                    hint="MHz values",
                    width=-1,
                    user_data=i,
                    callback=self._on_check_text,
                )
        for i, lk in enumerate(m.locks):
            with dpg.group(horizontal=True, parent=TAG_LOCKS):
                dpg.add_text(f"{format_mhz(lk.freq_hz):>9}", tag=f"coord.lock.row.{i}.mhz")
                dpg.add_input_text(
                    tag=self._dyn(f"coord.lock.row.{i}.label"),
                    default_value=lk.label,
                    width=130,
                    on_enter=True,
                    user_data=i,
                    callback=self._on_lock_label,
                )
                dpg.add_combo(
                    presets if lk.preset in presets else [lk.preset, *presets],
                    tag=f"coord.lock.row.{i}.preset",
                    default_value=lk.preset,
                    width=100,
                    user_data=i,
                    callback=self._on_lock_preset,
                )
                dpg.add_button(
                    label="X",
                    tag=f"coord.lock.row.{i}.remove",
                    user_data=i,
                    callback=self._on_remove_lock,
                )

    def _rebuild_results(self, result: CoordinationResult | None) -> None:
        for child in dpg.get_item_children(TAG_RESULTS, 1) or []:
            dpg.delete_item(child)
        if result is None:
            dpg.configure_item(TAG_RESULTS, show=False)
            return
        for cells in result_rows(result.plan):
            with dpg.table_row(parent=TAG_RESULTS):
                for c in cells:
                    dpg.add_text(c)
        # Devices without a frequency (the reasons are listed under the table).
        for u in result.plan.unassigned:
            with dpg.table_row(parent=TAG_RESULTS):
                for c in (u.label, "none", "-", "-", "-", "!"):
                    dpg.add_text(c, color=theme.WARN_COLOR)
        plan = result.plan
        dpg.configure_item(TAG_RESULTS, show=bool(plan.assignments or plan.unassigned))

    def _rebuild_check(self, outcome: CheckOutcome | None) -> None:
        for child in dpg.get_item_children(TAG_CHECK_TABLE, 1) or []:
            dpg.delete_item(child)
        violations = outcome.report.violations if outcome else ()
        for v in violations:
            with dpg.table_row(parent=TAG_CHECK_TABLE):
                for c in violation_cells(v):
                    dpg.add_text(c, wrap=150)
        dpg.configure_item(TAG_CHECK_TABLE, show=bool(violations))

    # --- per frame -------------------------------------------------------------------------

    def update(self) -> None:
        a, m = self._a, self._a.model
        self._set(TAG_PROGRESS, a.progress_text())
        names = a.profile_names()
        presets = a.preset_names()
        structure = (id(m), m.structure_version, tuple(names), tuple(presets))
        if structure != self._structure:
            self._structure = structure
            self._rebuild_rows(names, presets)
            self._cfg(TAG_LOCK_PRESET, items=presets)
            if not dpg.get_value(TAG_LOCK_PRESET):
                dpg.set_value(TAG_LOCK_PRESET, a.default_lock_preset())
        scan = a.controller.resolve_trace("max")
        label = None if scan is None else scan[1].label
        seen = (a.version, m.revision, a.running, label, a.stale, a.scan_changed)
        if seen == self._seen:
            return
        self._seen = seen
        self._update_texts(label)

    def _update_texts(self, scan_label: str | None) -> None:
        a, m = self._a, self._a.model
        o = m.options
        self._set(TAG_MESSAGE, a.message)
        self._cfg(TAG_MESSAGE, color=theme.ERROR_COLOR if a.message_is_error else theme.OK_COLOR)
        total = sum(r.quantity for r in m.rows)
        self._set(TAG_TOTAL, f"{total} device{'s' if total != 1 else ''}")
        for i, row in enumerate(m.rows):
            self._value(f"coord.device.{i}.qty", row.quantity)
            self._set(f"coord.check.row.{i}.name", f"{row.profile or '?'} ({row.quantity})")
        self._value(TAG_USE_SCAN, o.use_scan)
        if o.use_scan:
            info = f"using {scan_label}" if scan_label else "no scan data yet: not used"
        else:
            info = "coordinating without a scan"
        self._set(TAG_SCAN_INFO, info)
        self._value(TAG_THRESHOLD, o.threshold_db)
        self._value(TAG_GUARD, o.guard_khz)
        self._value(TAG_FORBIDDEN, o.allow_forbidden)
        self._value(TAG_SINGLE_GROUP, o.prefer_single_group)
        self._value(TAG_BUDGET, o.time_budget_s)
        self._value(TAG_BACKUPS, o.backups_per_profile)
        self._value(TAG_OVERRIDE, o.override_enabled)
        self._value(TAG_SCALE, o.override_scale)
        self._cfg("coord.override.fields", show=o.override_enabled)
        for name in SPACING_FIELDS:
            khz = o.override_khz.get(name)
            self._value(override_tag(name, "check"), khz is not None)
            if khz is not None:
                self._value(override_tag(name, "value"), khz)
            self._cfg(override_tag(name, "value"), enabled=khz is not None)
        running = a.running
        self._cfg(TAG_RUN, enabled=not running)
        self._cfg(TAG_CHECK_RUN, enabled=not running)
        self._cfg(TAG_CANCEL, show=running)

        result = a.result
        if result is not self._result:
            self._result = result
            self._rebuild_results(result)
        plan = result.plan if result else None
        self._set(TAG_STATS, stats_text(plan) if plan else "No plan yet. Press Coordinate.")
        if a.stale:
            note = "The setup changed since this plan was made; press Coordinate again"
        elif a.scan_changed:
            note = "Scan data has changed since this plan was made - re-run Coordinate to use it"
        else:
            note = ""
        self._set(TAG_STALE, note)
        self._cfg(TAG_STALE, color=theme.WARN_COLOR if a.stale else theme.MUTED_COLOR)
        self._set(TAG_WARNINGS, "\n".join(f"! {w}" for w in plan.warnings) if plan else "")
        self._set(TAG_UNASSIGNED, unassigned_text(plan) if plan else "")
        self._set(TAG_BACKUP_LIST, backups_text(plan) if plan else "")
        self._value(TAG_SHOW, a.show_on_spectrum)
        for tag in (TAG_EDIT, TAG_CLEAR, *(export_tag(f.key) for f in PLAN_EXPORTS)):
            self._cfg(tag, enabled=plan is not None)

        outcome = a.check_outcome
        if outcome is not self._outcome:
            self._outcome = outcome
            self._rebuild_check(outcome)
        self._set(TAG_CHECK_SUMMARY, check_summary(outcome) if outcome else "")
        self._cfg(
            TAG_CHECK_SUMMARY,
            color=theme.OK_COLOR if outcome is None or outcome.report.ok else theme.ERROR_COLOR,
        )
        self._set(
            TAG_CHECK_WARNINGS,
            "\n".join(f"! {w}" for w in outcome.report.warnings) if outcome else "",
        )


__all__ = [
    "CoordinationPanel",
    "backups_text",
    "check_summary",
    "result_rows",
    "stats_text",
    "unassigned_text",
    "violation_cells",
]
