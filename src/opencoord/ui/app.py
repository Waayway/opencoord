"""Dear PyGui application entry point (``opencoord``)."""

import argparse
import sys
from collections.abc import Sequence

from opencoord import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencoord", description="OpenCoord spectrum scanner")
    parser.add_argument("--version", action="version", version=f"opencoord {__version__}")
    parser.add_argument(
        "--simulator", action="store_true", help="use the simulated device (no-op for now)"
    )
    parser.add_argument(
        "--smoke-frames",
        type=int,
        metavar="N",
        default=None,
        help="render N frames, then exit 0 (CI and packaging smoke test)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import dearpygui.dearpygui as dpg

    title = f"OpenCoord {__version__}"
    dpg.create_context()
    try:
        dpg.create_viewport(title=title, width=1280, height=800)
        with dpg.window(tag="main", label=title):
            dpg.add_text("OpenCoord - pre-alpha")
        dpg.set_primary_window("main", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        frames = 0
        while dpg.is_dearpygui_running():
            dpg.render_dearpygui_frame()
            frames += 1
            if args.smoke_frames is not None and frames >= args.smoke_frames:
                break
    finally:
        dpg.destroy_context()
    return 0


if __name__ == "__main__":
    sys.exit(main())
