"""Keyboard shortcuts: Space start/stop, R reset max hold, M / P / N / Shift+N markers, and
Ctrl+S / Ctrl+Shift+S / Ctrl+O / Ctrl+E for sessions and exports (``bind_files``).

Shortcuts are ignored while a text or number field has keyboard focus, so typing a frequency
never starts a scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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


def bind(
    controller: Controller,
    text_inputs: Sequence[str],
    typing: Callable[[], bool] | None = None,
) -> None:
    """Register the key handlers (call once after the layout is built).

    ``text_inputs`` are the tags of the text/number fields; a shortcut does nothing while one
    of them is being edited. ``typing`` covers fields that are created later (rebuilt editors).
    """

    def make(shortcut: Shortcut) -> Callable[..., None]:
        def handler(*_: object) -> None:
            if any(dpg.is_item_active(tag) for tag in text_inputs):
                return
            if typing is not None and typing():
                return
            if dpg.is_key_down(dpg.mvKey_ModCtrl) or dpg.is_key_down(dpg.mvKey_ModAlt):
                return
            if dpg.is_key_down(dpg.mvKey_ModShift) == shortcut.shift:
                shortcut.action(controller)

        return handler

    with dpg.handler_registry(tag="shortcuts.handlers"):
        for s in SHORTCUTS:
            dpg.add_key_press_handler(_KEYS[s.key], callback=make(s))


def bind_files(actions: Mapping[str, Callable[[], object]]) -> None:
    """Ctrl+S (``save``), Ctrl+Shift+S (``save_as``), Ctrl+O (``open``), Ctrl+E (``export``).

    Unlike the plain-key shortcuts these also work while a text field is being edited.
    """
    keys = {"S": dpg.mvKey_S, "O": dpg.mvKey_O, "E": dpg.mvKey_E}

    def make(letter: str) -> Callable[..., None]:
        def handler(*_: object) -> None:
            if not dpg.is_key_down(dpg.mvKey_ModCtrl) or dpg.is_key_down(dpg.mvKey_ModAlt):
                return
            shift = dpg.is_key_down(dpg.mvKey_ModShift)
            name = {"S": "save_as" if shift else "save", "O": "open", "E": "export"}[letter]
            if not shift or letter == "S":
                actions[name]()

        return handler

    with dpg.handler_registry(tag="shortcuts.files"):
        for letter, key in keys.items():
            dpg.add_key_press_handler(key, callback=make(letter))


def help_text() -> str:
    return "   ".join(f"{s.label}: {s.description}" for s in SHORTCUTS)


__all__ = ["SHORTCUTS", "Shortcut", "bind", "help_text"]
