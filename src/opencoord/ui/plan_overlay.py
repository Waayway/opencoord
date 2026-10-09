"""Coordination plan on the spectrum: assigned frequencies and backups as vertical lines.

Assigned frequencies are one ``inf_line_series`` ("Plan", vermillion, thicker) with a pooled
``plot_annotation`` per device (``Profile #n``) near the top of the plot, staggered over a few
rows below the channel numbers so neighbours stay readable. Annotation pixel offsets do not help
here (a clamped annotation is pinned back inside the plot after its offset), so the rows are
placed in plot units from the current y limits and moved when the view is zoomed or panned.
Backups are a second, dimmer ``inf_line_series`` without labels. Both are in the legend, so they
are not mistaken for user markers (grey drag lines). Data is pushed only when
``CoordinationActions.version`` changes; labels outside the visible x range are hidden.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.coord.solver import MAX_DEVICES
from opencoord.ui import theme
from opencoord.ui.coordination_actions import CoordinationActions

TAG_PLAN = "spectrum.plan"
TAG_BACKUPS = "spectrum.plan.backups"
#: One label per possible device.
MAX_PLAN_LABELS = MAX_DEVICES
#: Label rows as fractions of the visible y span below its top (channel numbers sit above).
_LABEL_TOP = 0.09
_LABEL_ROW = 0.045
_LABEL_ROWS = 4


def plan_note_tag(i: int) -> str:
    return f"spectrum.plan.note.{i}"


def label_y(index: int, y_lo: float, y_hi: float) -> float:
    """Y (dBm) of the ``index``-th label: rows cycle so neighbours do not overlap."""
    return y_hi - (y_hi - y_lo) * (_LABEL_TOP + _LABEL_ROW * (index % _LABEL_ROWS))


class PlanOverlayView:
    def __init__(self, actions: CoordinationActions, x_axis: str, y_axis: str) -> None:
        self._a = actions
        self._x_axis = x_axis
        self._y_axis = y_axis
        self._version = -1
        self._limits: tuple[float, float, float, float] | None = None
        #: Frequency (MHz) shown in each label slot, ``None`` when unused.
        self._note_x: list[float | None] = [None] * MAX_PLAN_LABELS

    def build_series(self) -> None:
        """The plan and backup lines (call inside the spectrum's y axis)."""
        dpg.add_inf_line_series([], label="Plan", tag=TAG_PLAN, show=False)
        dpg.add_inf_line_series([], label="Backups", tag=TAG_BACKUPS, show=False)
        for tag, color, weight in (
            (TAG_PLAN, theme.PLAN_COLOR, 2.0),
            (TAG_BACKUPS, theme.PLAN_BACKUP_COLOR, 1.0),
        ):
            with dpg.theme() as line_theme, dpg.theme_component(dpg.mvInfLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, color, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(
                    dpg.mvPlotStyleVar_LineWeight, weight, category=dpg.mvThemeCat_Plots
                )
            dpg.bind_item_theme(tag, line_theme)

    def build_notes(self) -> None:
        """The label pool (call inside the plot)."""
        for i in range(MAX_PLAN_LABELS):
            dpg.add_plot_annotation(
                tag=plan_note_tag(i),
                offset=(4.0, 0.0),
                color=theme.PLAN_NOTE_COLOR,
                clamped=True,
                show=False,
            )

    def update(self) -> None:
        x_lo, x_hi = (float(v) for v in dpg.get_axis_limits(self._x_axis))
        y_lo, y_hi = (float(v) for v in dpg.get_axis_limits(self._y_axis))
        if self._a.version != self._version:
            self._version = self._a.version
            self._push()
            self._limits = None
        if self._limits != (x_lo, x_hi, y_lo, y_hi):
            self._limits = (x_lo, x_hi, y_lo, y_hi)
            for i, x in enumerate(self._note_x):
                shown = x is not None and x_lo <= x <= x_hi
                if shown:
                    dpg.set_value(plan_note_tag(i), (x, label_y(i, y_lo, y_hi)))
                dpg.configure_item(plan_note_tag(i), show=shown)

    def _push(self) -> None:
        assigned, backups = self._a.spectrum_lines()
        dpg.set_value(TAG_PLAN, [[f / 1e6 for f, _ in assigned]])
        dpg.configure_item(TAG_PLAN, show=bool(assigned))
        dpg.set_value(TAG_BACKUPS, [[f / 1e6 for f in backups]])
        dpg.configure_item(TAG_BACKUPS, show=bool(backups))
        for i in range(MAX_PLAN_LABELS):
            if i >= len(assigned):
                self._note_x[i] = None
                continue
            f, label = assigned[i]
            self._note_x[i] = f / 1e6
            dpg.configure_item(plan_note_tag(i), label=label)


__all__ = ["MAX_PLAN_LABELS", "PlanOverlayView", "label_y", "plan_note_tag"]
