"""Keyboard shortcuts: Space start/stop, R reset max hold, M / P / N / Shift+N markers.

Shortcuts are ignored while a text or number field has keyboard focus, so typing a frequency
never starts a scan.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import dearpygui.dearpygui as dpg

from opencoord.ui.controller import Controller


@dataclass(frozen=True)
class Shortcut:
    key: str
    description: str
    action: Callable[[Controller], object]
    #: Fires only with Shift held (``True``) or only without (``False``).
    shift: bool = False

    @property
    def label(self) -> str:
        return f"Shift+{self.key}" if self.shift else self.key


SHORTCUTS: tuple[Shortcut, ...] = (
    Shortcut("Space", "Start / stop", Controller.toggle),
    Shortcut("R", "Reset max hold", Controller.reset_max_hold),
    Shortcut("M", "Marker at cursor (or peak)", Controller.add_marker_at_cursor),
    Shortcut("P", "Selected marker to peak", Controller.marker_to_peak),
    Shortcut("N", "Next peak right", lambda c: c.marker_next_peak("right")),
    Shortcut("N", "Next peak left", lambda c: c.marker_next_peak("left"), shift=True),
)
_KEYS = {
    "Space": dpg.mvKey_Spacebar,
    "R": dpg.mvKey_R,
    "M": dpg.mvKey_M,
    "P": dpg.mvKey_P,
    "N": dpg.mvKey_N,
}


def bind(controller: Controller, text_inputs: Sequence[str]) -> None:
    """Register the key handlers (call once after the layout is built).

    ``text_inputs`` are the tags of the text/number fields; a shortcut does nothing while one
    of them is being edited.
    """

    def make(shortcut: Shortcut) -> Callable[..., None]:
        def handler(*_: object) -> None:
            if any(dpg.is_item_active(tag) for tag in text_inputs):
                return
            if dpg.is_key_down(dpg.mvKey_ModCtrl) or dpg.is_key_down(dpg.mvKey_ModAlt):
                return
            if dpg.is_key_down(dpg.mvKey_ModShift) == shortcut.shift:
                shortcut.action(controller)

        return handler

    with dpg.handler_registry(tag="shortcuts.handlers"):
        for s in SHORTCUTS:
            dpg.add_key_press_handler(_KEYS[s.key], callback=make(s))


def help_text() -> str:
    return "   ".join(f"{s.label}: {s.description}" for s in SHORTCUTS)


__all__ = ["SHORTCUTS", "Shortcut", "bind", "help_text"]
