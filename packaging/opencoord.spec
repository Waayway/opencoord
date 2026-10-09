# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for OpenCoord (onedir). Run through `uv run python packaging/build.py`,
# or directly: `uv run pyinstaller --noconfirm packaging/opencoord.spec`.
# On macOS it also produces OpenCoord.app via BUNDLE.
import re
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
ICONS = ROOT / "packaging" / "icons"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
# Info.plist versions must be numeric: "0.1.0.dev0" -> "0.1.0".
PLIST_VERSION = re.match(r"\d+(?:\.\d+){0,2}", VERSION).group(0)

if sys.platform == "win32":
    icon = str(ICONS / "opencoord.ico")
elif sys.platform == "darwin":
    icon = str(ICONS / "opencoord.icns")
else:
    icon = None  # Linux: the icon ships via the AppImage / .desktop file instead

datas = [
    # importlib.metadata.version("opencoord") must work when frozen (opencoord.__version__).
    *copy_metadata("opencoord"),
    # Package data: channel plans, profile templates, spacing presets.
    *collect_data_files("opencoord", includes=["**/*.toml"]),
]

a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "opencoord" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=collect_dynamic_libs("dearpygui"),
    datas=datas,
    hiddenimports=collect_submodules("dearpygui") + collect_submodules("opencoord"),
    excludes=["tkinter"],
    noarchive=False,
)
if sys.platform.startswith("linux"):
    # Use the host's copies of these (as the AppImage excludelist does). A bundled libstdc++ from an
    # older build distro breaks the host's Mesa drivers (GLX: "No GLXFBConfigs returned"), and the
    # X11/GL stack must match the host's anyway. Every desktop that can run OpenGL ships them.
    HOST_LIBS = ("libstdc++.so", "libgcc_s.so", "libX11", "libXau.so", "libXdmcp.so", "libxcb", "libGL")
    a.binaries = [b for b in a.binaries if not Path(b[0]).name.startswith(HOST_LIBS)]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenCoord",
    debug=False,
    strip=False,
    upx=False,
    # Windowed on Windows/macOS (no console window); a console app on Linux so --version prints.
    console=sys.platform not in ("win32", "darwin"),
    icon=icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OpenCoord",
)

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="OpenCoord.app",
        icon=icon,
        bundle_identifier="io.github.waayway.opencoord",
        version=PLIST_VERSION,
        info_plist={
            "CFBundleName": "OpenCoord",
            "CFBundleDisplayName": "OpenCoord",
            "CFBundleShortVersionString": PLIST_VERSION,
            "CFBundleVersion": PLIST_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",  # the locked dearpygui wheels are macosx_13_0_arm64
            "NSHumanReadableCopyright": "GPL-3.0-or-later",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "OpenCoord session",
                    "CFBundleTypeRole": "Editor",
                    "CFBundleTypeExtensions": ["opencoord"],
                    "LSHandlerRank": "Owner",
                }
            ],
        },
    )
