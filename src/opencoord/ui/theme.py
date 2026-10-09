"""Dark theme and the colour-blind-safe trace palette (Okabe-Ito)."""

from __future__ import annotations

import dearpygui.dearpygui as dpg

RGBA = tuple[int, int, int, int]

#: Trace colours by series key (Okabe-Ito palette, distinguishable with colour blindness).
TRACE_COLORS: dict[str, RGBA] = {
    "live": (86, 180, 233, 255),  # sky blue
    "max": (230, 159, 0, 255),  # orange
    "avg": (0, 158, 115, 255),  # bluish green
    "min": (204, 121, 167, 255),  # reddish purple
    "scan": (240, 228, 66, 255),  # yellow
    # Frozen reference traces: muted, so they sit behind the live data.
    "ref1": (150, 160, 175, 170),
    "ref2": (170, 150, 175, 170),
    "ref3": (150, 175, 160, 170),
    "ref4": (180, 165, 140, 170),
}
MARKER_COLOR: RGBA = (210, 210, 220, 200)
MARKER_SELECTED_COLOR: RGBA = (255, 255, 255, 255)
THRESHOLD_COLOR: RGBA = (255, 110, 90, 220)
ERROR_COLOR: RGBA = (255, 110, 90, 255)
OK_COLOR: RGBA = (0, 190, 140, 255)
MUTED_COLOR: RGBA = (150, 150, 160, 255)
WARN_COLOR: RGBA = (255, 190, 80, 255)
#: Coordination plan on the spectrum (Okabe-Ito vermillion; no trace uses it).
PLAN_COLOR: RGBA = (213, 94, 0, 235)
PLAN_BACKUP_COLOR: RGBA = (213, 94, 0, 110)
PLAN_NOTE_COLOR: RGBA = (110, 48, 0, 230)

_BG: RGBA = (22, 24, 28, 255)
_PANEL: RGBA = (30, 33, 38, 255)
_FRAME: RGBA = (44, 48, 56, 255)
_ACCENT: RGBA = (40, 110, 170, 255)


def apply() -> None:
    """Create the global dark theme and bind it (call once after ``create_context``)."""
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, _BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, _FRAME)
            dpg.add_theme_color(dpg.mvThemeCol_Button, _FRAME)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Header, _FRAME)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_TabActive, _ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, _ACCENT)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
        with dpg.theme_component(dpg.mvAll, enabled_state=False):
            dpg.add_theme_color(dpg.mvThemeCol_Text, MUTED_COLOR)
            dpg.add_theme_color(dpg.mvThemeCol_Button, _PANEL)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _PANEL)
        with dpg.theme_component(dpg.mvPlot):
            dpg.add_theme_color(
                dpg.mvPlotCol_PlotBg, (12, 13, 16, 255), category=dpg.mvThemeCat_Plots
            )
            dpg.add_theme_style(dpg.mvPlotStyleVar_PlotPadding, 6, 6, category=dpg.mvThemeCat_Plots)
    dpg.bind_theme(global_theme)


def series_theme(key: str) -> int | str:
    """A theme colouring a line series in the palette colour for ``key``."""
    theme: int | str
    with dpg.theme() as theme, dpg.theme_component(dpg.mvLineSeries):
        dpg.add_theme_color(dpg.mvPlotCol_Line, TRACE_COLORS[key], category=dpg.mvThemeCat_Plots)
        weight = 2.0 if key == "scan" else 1.0
        dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight, weight, category=dpg.mvThemeCat_Plots)
    return theme


__all__ = [
    "ERROR_COLOR",
    "MARKER_COLOR",
    "MARKER_SELECTED_COLOR",
    "MUTED_COLOR",
    "OK_COLOR",
    "PLAN_BACKUP_COLOR",
    "PLAN_COLOR",
    "PLAN_NOTE_COLOR",
    "THRESHOLD_COLOR",
    "TRACE_COLORS",
    "WARN_COLOR",
    "apply",
    "series_theme",
]
