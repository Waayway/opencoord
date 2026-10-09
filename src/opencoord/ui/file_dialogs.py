"""Dear PyGui side of sessions and exports: File menu, file dialogs, export window, plot capture.

All behaviour lives in :class:`opencoord.ui.files.FileActions`; this module only asks the user for
paths and, for the PNG export, grabs the rendered frame (``output_frame_buffer``) and crops it to
the plot area (spectrum + waterfall).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import dearpygui.dearpygui as dpg
import numpy as np
import numpy.typing as npt

from opencoord.ui.files import EXPORT_FORMATS, SESSION_SUFFIX, FileActions, export_format

log = logging.getLogger(__name__)

TAG_EXPORT_WINDOW: Final = "files.export"
TAG_EXPORT_FORMAT: Final = "files.export.format"
TAG_EXPORT_TRACE: Final = "files.export.trace"
#: Frames to wait after the dialog closed before grabbing the frame (so it is not in the image).
CAPTURE_DELAY_FRAMES: Final = 4
_DIALOG_SIZE: Final = (760, 460)


def crop_to_rect(
    frame: npt.NDArray[np.uint8], x: float, y: float, w: float, h: float
) -> npt.NDArray[np.uint8]:
    """The ``(x, y, w, h)`` pixel rectangle of ``frame`` (height, width, 4), clamped to its bounds;
    the whole frame when the rectangle is empty or outside it."""
    height, width = frame.shape[:2]
    x0, y0 = max(int(x), 0), max(int(y), 0)
    x1, y1 = min(int(x + w), width), min(int(y + h), height)
    if x1 <= x0 or y1 <= y0:
        return frame
    return frame[y0:y1, x0:x1]


def frame_to_rgba(buffer: Any, width: int) -> npt.NDArray[np.uint8]:
    """RGBA ``uint8`` rows from the frame buffer Dear PyGui hands to the callback (float 0..1)."""
    values = np.frombuffer(buffer, dtype=np.float32)
    if values.size % 4 or width <= 0 or (values.size // 4) % width:
        raise ValueError("Unexpected frame buffer size")
    pixels = values.reshape(-1, width, 4)
    return (np.clip(pixels, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


class FileUI:
    def __init__(
        self, files: FileActions, plot_rect: Callable[[], tuple[float, float, float, float]]
    ) -> None:
        """``plot_rect()`` is the ``(x, y, width, height)`` of the plot area in frame pixels."""
        self.files = files
        self._plot_rect = plot_rect
        self._dialogs = 0
        self._capture: tuple[int, Path] | None = None
        self._title = ""

    # --- layout ------------------------------------------------------------------------------

    def build_menu(self) -> None:
        """The File menu; call inside the main window's ``menu_bar``."""
        with dpg.menu(label="File"):
            dpg.add_menu_item(
                label="Open session...", shortcut="Ctrl+O", callback=self.open_session
            )
            dpg.add_menu_item(label="Save", shortcut="Ctrl+S", callback=self.save)
            dpg.add_menu_item(label="Save as...", shortcut="Ctrl+Shift+S", callback=self.save_as)
            dpg.add_separator()
            dpg.add_menu_item(label="Export...", shortcut="Ctrl+E", callback=self.export_dialog)
            dpg.add_menu_item(label="Import scan as reference...", callback=self.import_reference)

    def build_windows(self) -> None:
        """The export window (hidden until Ctrl+E); call once after the main layout."""
        with dpg.window(
            label="Export",
            tag=TAG_EXPORT_WINDOW,
            show=False,
            modal=True,
            no_resize=True,
            width=430,
            height=190,
        ):
            dpg.add_text("Format")
            dpg.add_combo(
                [f.label for f in EXPORT_FORMATS],
                default_value=EXPORT_FORMATS[0].label,
                tag=TAG_EXPORT_FORMAT,
                width=-1,
            )
            dpg.add_text("Trace")
            dpg.add_combo([], tag=TAG_EXPORT_TRACE, width=-1)
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Choose file...", width=140, callback=self._export_chosen)
                dpg.add_button(
                    label="Cancel",
                    width=80,
                    callback=lambda: dpg.configure_item(TAG_EXPORT_WINDOW, show=False),
                )

    # --- actions -----------------------------------------------------------------------------

    def open_session(self) -> None:
        self._ask("Open session", [SESSION_SUFFIX], self.files.open)

    def save(self) -> None:
        if self.files.path is None:
            self.save_as()
        else:
            self.files.save()

    def save_as(self) -> None:
        name = self.files.path.name if self.files.path else f"session{SESSION_SUFFIX}"
        self._ask("Save session as", [SESSION_SUFFIX], self.files.save, default_name=name)

    def import_reference(self) -> None:
        self._ask("Import scan as reference", [".csv", ".txt", ".*"], self.files.import_reference)

    def export_dialog(self) -> None:
        choices = self.files.trace_choices()
        dpg.configure_item(TAG_EXPORT_TRACE, items=[f"{k}: {label}" for k, label in choices])
        if choices:
            dpg.set_value(TAG_EXPORT_TRACE, f"{choices[0][0]}: {choices[0][1]}")
        dpg.configure_item(TAG_EXPORT_WINDOW, show=True)

    def _export_chosen(self) -> None:
        fmt = next(f for f in EXPORT_FORMATS if f.label == dpg.get_value(TAG_EXPORT_FORMAT))
        key = str(dpg.get_value(TAG_EXPORT_TRACE)).split(":", 1)[0]
        dpg.configure_item(TAG_EXPORT_WINDOW, show=False)
        self._ask(
            f"Export {fmt.label}",
            [fmt.suffix],
            lambda path: self._export_to(fmt.key, key, path),
            default_name=f"opencoord{fmt.suffix}",
        )

    def _export_to(self, fmt_key: str, trace_key: str, path: Path) -> bool:
        if export_format(fmt_key).key == "png":
            self._capture = (CAPTURE_DELAY_FRAMES, path)
            return True
        return self.files.export(fmt_key, trace_key, path)

    # --- dialogs -----------------------------------------------------------------------------

    def _ask(
        self,
        title: str,
        extensions: list[str],
        action: Callable[[Path], object],
        *,
        default_name: str = "",
    ) -> None:
        self._dialogs += 1
        tag = f"files.dialog.{self._dialogs}"

        def done(sender: object, app_data: dict[str, Any]) -> None:
            dpg.delete_item(tag)
            selected = app_data.get("file_path_name") or ""
            if selected and not selected.endswith(("/", "\\")):
                action(Path(selected))

        with dpg.file_dialog(
            label=title,
            tag=tag,
            modal=True,
            width=_DIALOG_SIZE[0],
            height=_DIALOG_SIZE[1],
            default_filename=default_name,
            callback=done,
            cancel_callback=lambda *_: dpg.delete_item(tag),
        ):
            for ext in extensions:
                dpg.add_file_extension(ext)

    # --- per frame ---------------------------------------------------------------------------

    def update(self) -> None:
        """Called once per frame: window title and the delayed plot capture."""
        title = self.files.title
        if title != self._title:
            self._title = title
            dpg.set_viewport_title(title)
        if self._capture is None:
            return
        frames, path = self._capture
        if frames > 0:
            self._capture = (frames - 1, path)
            return
        self._capture = None
        dpg.output_frame_buffer(callback=lambda _s, buf: self._save_png(path, buf))

    def _save_png(self, path: Path, buffer: Any) -> None:
        try:
            frame = frame_to_rgba(buffer, dpg.get_viewport_client_width())
            rgba = crop_to_rect(frame, *self._plot_rect())
        except (ValueError, KeyError, SystemError):
            log.warning("could not read the frame buffer", exc_info=True)
            self.files.say("Cannot read the screen image; the PNG export failed")
            return
        self.files.export("png", "", path, np.ascontiguousarray(rgba))
