"""Waterfall: the ``WaterfallHistory`` rows colour-mapped into a dynamic texture.

The texture is ``DISPLAY_BINS`` wide and ``depth`` rows high, newest row on top, drawn with an
``image_series`` in a plot whose x axis (MHz) is linked to the spectrum. Rows are mapped through a
precomputed 256-entry LUT; NaN (no data) uses the background colour. The texture is rebuilt only
when the history version changes, and only the new rows are colour-mapped. The y axis is
limited to the filled rows (at least ``MIN_VISIBLE_ROWS``), so the history grows downwards.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import numpy as np
import numpy.typing as npt

from opencoord.ui.state import AppState, WaterfallHistory

#: Colour scale of the waterfall in dBm (Phase 5 makes it adjustable).
LEVEL_MIN_DBM = -115.0
LEVEL_MAX_DBM = -35.0
LUT_SIZE = 256
#: The y axis shows the filled rows, but at least this many, so that the first rows (and the
#: one row each scan adds) are visible instead of a sliver at the top of a 300-row texture.
MIN_VISIBLE_ROWS = 20
BACKGROUND = (0.05, 0.05, 0.06, 1.0)
# Viridis control points (perceptually uniform and colour-blind safe), dark to bright.
_STOPS = np.array(
    [
        (0.267, 0.005, 0.329),
        (0.283, 0.141, 0.458),
        (0.254, 0.265, 0.530),
        (0.207, 0.372, 0.553),
        (0.164, 0.471, 0.558),
        (0.128, 0.567, 0.551),
        (0.135, 0.659, 0.518),
        (0.267, 0.749, 0.441),
        (0.478, 0.821, 0.318),
        (0.741, 0.873, 0.150),
        (0.993, 0.906, 0.144),
    ]
)

TAG_TEXTURE = "waterfall.texture"
TAG_PLOT = "waterfall.plot"
TAG_X = "waterfall.x"
TAG_Y = "waterfall.y"
TAG_IMAGE = "waterfall.image"


def build_lut(size: int = LUT_SIZE) -> npt.NDArray[np.float32]:
    """``size`` RGBA colours from low to high level, plus the background at index ``size``."""
    pos = np.linspace(0.0, 1.0, len(_STOPS))
    x = np.linspace(0.0, 1.0, size)
    rgb = np.stack([np.interp(x, pos, _STOPS[:, c]) for c in range(3)], axis=1)
    lut = np.ones((size + 1, 4), dtype=np.float32)
    lut[:size, :3] = rgb
    lut[size] = BACKGROUND
    return lut


def to_rgba(
    rows: npt.NDArray[np.float32], lo_dbm: float, hi_dbm: float, lut: npt.NDArray[np.float32]
) -> npt.NDArray[np.float32]:
    """Map dBm ``rows`` (h, w) to RGBA (h, w, 4) through ``lut`` (NaN = background)."""
    n = len(lut) - 1
    scaled = (rows - lo_dbm) * ((n - 1) / (hi_dbm - lo_dbm))
    idx = np.clip(np.nan_to_num(scaled, nan=0.0), 0, n - 1).astype(np.intp)
    idx[np.isnan(rows)] = n
    rgba: npt.NDArray[np.float32] = lut[idx]
    return rgba


def visible_rows(count: int, depth: int) -> int:
    """Rows the y axis shows: the filled ones, at least ``MIN_VISIBLE_ROWS``, at most all."""
    return min(depth, max(count, MIN_VISIBLE_ROWS))


class WaterfallView:
    def __init__(self) -> None:
        self._lut = build_lut()
        self._rgba: npt.NDArray[np.float32] | None = None
        self._version = -1
        self._pushes = 0
        self._generation = -1
        self._range: tuple[int, int] | None = None
        self._depth = 0
        self._visible = 0

    def build(self, history: WaterfallHistory) -> None:
        """Add the texture and the plot (call inside the spectrum/waterfall subplots)."""
        self._depth = history.depth
        self._rgba = to_rgba(history.rows, LEVEL_MIN_DBM, LEVEL_MAX_DBM, self._lut)
        with dpg.texture_registry():
            dpg.add_dynamic_texture(
                history.bins, history.depth, self._rgba.ravel(), tag=TAG_TEXTURE
            )
        with dpg.plot(tag=TAG_PLOT, no_title=True, no_menus=True, no_box_select=True):
            dpg.add_plot_axis(dpg.mvXAxis, tag=TAG_X, label="Frequency (MHz)")
            with dpg.plot_axis(dpg.mvYAxis, tag=TAG_Y, label="Sweeps", no_tick_labels=True):
                dpg.add_image_series(TAG_TEXTURE, (470.0, 0.0), (960.0, 1.0), tag=TAG_IMAGE)
        self._set_visible(history)

    def _set_visible(self, h: WaterfallHistory) -> None:
        visible = visible_rows(h.count, h.depth)
        if visible != self._visible:
            dpg.set_axis_limits(TAG_Y, h.depth - visible, h.depth)
            self._visible = visible

    def update(self, state: AppState) -> None:
        h = state.waterfall
        if h.version == self._version:
            return
        if h.depth != self._depth:
            self._rebuild_texture(h)
        new_rows = h.pushes - self._pushes
        if self._rgba is None or h.generation != self._generation or new_rows >= h.depth:
            self._rgba = to_rgba(h.rows, LEVEL_MIN_DBM, LEVEL_MAX_DBM, self._lut)
        elif new_rows > 0:
            self._rgba[new_rows:] = self._rgba[:-new_rows]
            self._rgba[:new_rows] = to_rgba(
                h.rows[:new_rows], LEVEL_MIN_DBM, LEVEL_MAX_DBM, self._lut
            )
        assert self._rgba is not None
        dpg.set_value(TAG_TEXTURE, self._rgba.ravel())
        if h.range_hz is not None and h.range_hz != self._range:
            lo, hi = h.range_hz
            dpg.configure_item(
                TAG_IMAGE, bounds_min=(lo / 1e6, 0.0), bounds_max=(hi / 1e6, h.depth)
            )
        self._set_visible(h)
        self._range = h.range_hz
        self._version = h.version
        self._pushes = h.pushes
        self._generation = h.generation

    def _rebuild_texture(self, h: WaterfallHistory) -> None:
        dpg.delete_item(TAG_IMAGE)
        dpg.delete_item(TAG_TEXTURE)
        self._rgba = to_rgba(h.rows, LEVEL_MIN_DBM, LEVEL_MAX_DBM, self._lut)
        with dpg.texture_registry():
            dpg.add_dynamic_texture(h.bins, h.depth, self._rgba.ravel(), tag=TAG_TEXTURE)
        lo, hi = h.range_hz or (470_000_000, 960_000_000)
        dpg.add_image_series(
            TAG_TEXTURE, (lo / 1e6, 0.0), (hi / 1e6, h.depth), tag=TAG_IMAGE, parent=TAG_Y
        )
        self._visible = 0
        self._depth = h.depth
        self._range = None  # force the bounds update


__all__ = [
    "LEVEL_MAX_DBM",
    "LEVEL_MIN_DBM",
    "MIN_VISIBLE_ROWS",
    "WaterfallView",
    "build_lut",
    "to_rgba",
    "visible_rows",
]
