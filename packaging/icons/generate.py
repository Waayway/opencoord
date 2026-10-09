# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=11", "resvg-py>=0.2"]
# ///
"""Render ``opencoord.svg`` into the PNG / ICO / ICNS icons used by the packagers.

Run from anywhere (outputs are committed, so this is only needed after editing the SVG):

    uv run packaging/icons/generate.py
"""

import io
from pathlib import Path

import resvg_py
from PIL import Image

HERE = Path(__file__).resolve().parent
SVG = HERE / "opencoord.svg"

PNG_SIZES = (16, 32, 48, 64, 128, 256, 512)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_BASE = 1024  # Pillow derives every smaller .icns size from this one


def render(size: int) -> Image.Image:
    data = resvg_py.svg_to_bytes(svg_path=str(SVG), width=size, height=size)
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def main() -> None:
    for size in PNG_SIZES:
        render(size).save(HERE / f"opencoord-{size}.png", optimize=True)
    # The unsuffixed PNG is what .desktop files / AppDirs reference (Icon=opencoord).
    render(256).save(HERE / "opencoord.png", optimize=True)

    largest = render(max(ICO_SIZES))
    largest.save(HERE / "opencoord.ico", sizes=[(s, s) for s in ICO_SIZES])

    render(ICNS_BASE).save(HERE / "opencoord.icns")

    for path in sorted(HERE.glob("opencoord*.*")):
        if path.suffix != ".svg":
            print(path.relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
