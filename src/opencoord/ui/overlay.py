"""Spectrum overlay: channel plan grid, band colouring, channel occupancy, exclusion zones.

Everything is pooled and pushed only when its inputs changed:

* **grid**: one ``inf_line_series`` with the channel edges (MHz);
* **bands**: translucent ``draw_rectangle`` items on a ``draw_layer`` inside the plot (plot
  coordinates; the rectangles are tall so they cover any y range; they never take part in the
  plot's fit). Allowed = green, forbidden = red, info = blue;
* **channel numbers**: one clamped ``plot_annotation`` per channel at the top edge of the plot, its
  background coloured by occupancy (free / some bins above the threshold / mostly occupied).
  Annotations are clamped to the plot, so ones outside the visible x range are hidden;
* **exclusion zones**: dark rectangles with an ``Xn`` label at the bottom edge. They are always
  drawn, whether or not the channel overlay is on. (ImPlot has no hatching.)
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from opencoord.coord.channel_plans import Pmse
from opencoord.core.analysis import Analysis
from opencoord.core.occupancy import PARTIAL_COVERAGE, ChannelOccupancy
from opencoord.core.zones import MAX_EXCLUSION_ZONES
from opencoord.ui.controller import Controller
from opencoord.ui.state import AppState
from opencoord.ui.theme import RGBA

TAG_LAYER = "overlay.layer"
TAG_GRID = "overlay.grid"
#: Most channels / shaded spans the overlay can show (the EU plan has 28 / 7).
MAX_CHANNELS = 64
MAX_SPANS = 24
#: Rectangles span this many dBm either side of 0 (the plot clips them).
_TALL = 1000.0
#: Annotation y: far above any plot, so the clamp pins it to the top edge.
_TOP_Y = 1000.0

SPAN_COLORS: dict[Pmse, RGBA] = {
    "allowed": (0, 190, 110, 34),
    "forbidden": (225, 60, 50, 44),
    "info": (70, 140, 230, 40),
}
ZONE_FILL: RGBA = (0, 0, 0, 70)
GRID_COLOR: RGBA = (200, 200, 210, 70)
#: Channel badge colours by occupancy.
FREE_COLOR: RGBA = (40, 110, 90, 235)
SOME_COLOR: RGBA = (200, 140, 0, 235)
BUSY_COLOR: RGBA = (205, 70, 50, 235)
PLAIN_COLOR: RGBA = (70, 74, 84, 235)  # no occupancy data yet
#: Percent of bins above the threshold: below ``SOME`` = free, up to ``BUSY`` = some, else busy.
SOME_PERCENT = 1.0
BUSY_PERCENT = 25.0


def occupancy_color(percent_above: float | None, coverage: float = 1.0) -> RGBA:
    """Badge colour: grey without data or when the channel is only partly covered (no verdict),
    else green free, amber some, red busy."""
    if percent_above is None or coverage < PARTIAL_COVERAGE:
        return PLAIN_COLOR
    if percent_above < SOME_PERCENT:
        return FREE_COLOR
    return SOME_COLOR if percent_above < BUSY_PERCENT else BUSY_COLOR


def channel_tag(i: int) -> str:
    return f"overlay.channel.{i}"


def span_tag(i: int) -> str:
    return f"overlay.span.{i}"


def zone_tag(i: int) -> str:
    return f"overlay.zone.{i}"


def zone_note_tag(i: int) -> str:
    return f"overlay.zone.{i}.note"


class OverlayView:
    def __init__(self, controller: Controller, x_axis: str) -> None:
        self._c = controller
        self._x_axis = x_axis
        #: What the grid, spans and zones were drawn from; the badge colours have their own key.
        self._static_key: tuple[object, ...] | None = None
        self._analysis: Analysis | None = None
        self._channels: tuple[int, ...] = ()
        self._limits: tuple[float, float] | None = None
        #: Centre (MHz) of the channel / zone shown in each annotation slot, ``None`` when unused.
        self._channel_x: list[float | None] = [None] * MAX_CHANNELS
        self._zone_x: list[float | None] = [None] * MAX_EXCLUSION_ZONES

    def build_grid(self) -> None:
        """Add the channel edge lines (call inside the y axis, before the traces)."""
        dpg.add_inf_line_series([], tag=TAG_GRID, show=False)
        with dpg.theme() as grid_theme, dpg.theme_component(dpg.mvInfLineSeries):
            dpg.add_theme_color(dpg.mvPlotCol_Line, GRID_COLOR, category=dpg.mvThemeCat_Plots)
            dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight, 1.0, category=dpg.mvThemeCat_Plots)
        dpg.bind_item_theme(TAG_GRID, grid_theme)

    def build_layers(self) -> None:
        """Add the band / zone rectangles and the channel / zone labels (inside the plot)."""
        with dpg.draw_layer(tag=TAG_LAYER):
            for i in range(MAX_SPANS):
                dpg.draw_rectangle(
                    (0, -_TALL), (1, _TALL), tag=span_tag(i), color=(0, 0, 0, 0), show=False
                )
            for i in range(MAX_EXCLUSION_ZONES):
                dpg.draw_rectangle(
                    (0, -_TALL),
                    (1, _TALL),
                    tag=zone_tag(i),
                    color=(0, 0, 0, 0),  # an outline of a tall rectangle renders as a fat bar
                    fill=ZONE_FILL,
                    show=False,
                )
        for i in range(MAX_CHANNELS):
            dpg.add_plot_annotation(
                tag=channel_tag(i), default_value=(0.0, _TOP_Y), offset=(0, 12), show=False
            )
        for i in range(MAX_EXCLUSION_ZONES):
            dpg.add_plot_annotation(
                tag=zone_note_tag(i),
                default_value=(0.0, -_TOP_Y),
                offset=(0, -12),
                color=(40, 40, 46, 235),
                show=False,
            )

    # --- per frame ---

    def update(self, state: AppState) -> None:
        analysis = self._c.analysis() if state.overlay_enabled else None
        plan = state.channel_plan
        static_key = (
            state.overlay_enabled,
            plan.name if plan else None,
            tuple(state.exclusion_zones),
        )
        limits = tuple(dpg.get_axis_limits(self._x_axis))
        lo, hi = float(limits[0]), float(limits[1])
        if static_key != self._static_key:
            self._static_key = static_key
            self._push_static(state)
            self._push_badges(analysis)
            self._analysis = analysis
            self._limits = None
        elif analysis is not self._analysis:
            self._analysis = analysis
            self._push_badges(analysis)
        if self._limits != (lo, hi):
            self._limits = (lo, hi)
            self._show_visible(state, lo, hi)

    def _push_badges(self, analysis: Analysis | None) -> None:
        """Colour the channel numbers by occupancy (only they change with the data)."""
        occupancy: dict[int, ChannelOccupancy] = (
            {o.number: o for o in analysis.occupancy} if analysis else {}
        )
        for i, number in enumerate(self._channels):
            occ = occupancy.get(number)
            dpg.configure_item(
                channel_tag(i),
                color=occupancy_color(
                    occ.percent_above if occ else None, occ.coverage if occ else 1.0
                ),
            )

    def _push_static(self, state: AppState) -> None:
        on, plan = state.overlay_enabled, state.channel_plan
        channels = plan.channels[:MAX_CHANNELS] if on and plan else ()
        self._channels = tuple(c.number for c in channels)
        grid: list[float] = []
        for i in range(MAX_CHANNELS):
            if i >= len(channels):
                self._channel_x[i] = None
                continue
            ch = channels[i]
            grid.extend((ch.start_hz / 1e6, ch.stop_hz / 1e6))
            self._channel_x[i] = ch.centre_hz / 1e6
            dpg.set_value(channel_tag(i), (ch.centre_hz / 1e6, _TOP_Y))
            dpg.configure_item(channel_tag(i), label=str(ch.number))
        dpg.set_value(TAG_GRID, [sorted(set(grid))])
        dpg.configure_item(TAG_GRID, show=bool(grid))
        spans = plan.shaded_spans()[:MAX_SPANS] if on and plan else []
        for i in range(MAX_SPANS):
            if i >= len(spans):
                dpg.configure_item(span_tag(i), show=False)
                continue
            s = spans[i]
            dpg.configure_item(
                span_tag(i),
                pmin=(s.start_hz / 1e6, -_TALL),
                pmax=(s.stop_hz / 1e6, _TALL),
                fill=SPAN_COLORS[s.pmse],
                show=True,
            )
        zones = state.exclusion_zones
        for i in range(MAX_EXCLUSION_ZONES):
            if i >= len(zones):
                self._zone_x[i] = None
                dpg.configure_item(zone_tag(i), show=False)
                continue
            z = zones[i]
            lo, hi = z.start_hz / 1e6, z.stop_hz / 1e6
            self._zone_x[i] = (lo + hi) / 2
            dpg.configure_item(zone_tag(i), pmin=(lo, -_TALL), pmax=(hi, _TALL), show=True)
            dpg.set_value(zone_note_tag(i), ((lo + hi) / 2, -_TOP_Y))
            dpg.configure_item(zone_note_tag(i), label=f"X{z.id}")

    def _show_visible(self, _state: AppState, lo: float, hi: float) -> None:
        """Show an annotation only while its centre is inside the plot (clamping would pin it)."""
        for i, x in enumerate(self._channel_x):
            dpg.configure_item(channel_tag(i), show=x is not None and lo <= x <= hi)
        for i, x in enumerate(self._zone_x):
            dpg.configure_item(zone_note_tag(i), show=x is not None and lo <= x <= hi)


__all__ = ["OverlayView", "occupancy_color"]
