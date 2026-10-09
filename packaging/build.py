"""Build the native OpenCoord bundle and (optionally) its OS package.

    uv run python packaging/build.py               # PyInstaller onedir only
    uv run python packaging/build.py --appimage    # Linux:   + .tar.gz + .AppImage
    uv run python packaging/build.py --installer   # Windows: + portable .zip + Inno Setup installer
    uv run python packaging/build.py --dmg         # macOS:   + .dmg

The onedir lands in dist/pyinstaller/OpenCoord/ (and dist/pyinstaller/OpenCoord.app on macOS);
packaged artifacts land directly in dist/. Artifact paths are printed at the end.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUNDLE_DIST = DIST / "pyinstaller"
ONEDIR = BUNDLE_DIST / "OpenCoord"
APP = BUNDLE_DIST / "OpenCoord.app"


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def machine_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def pyinstaller() -> None:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(BUNDLE_DIST),
            "--workpath",
            str(ROOT / "build" / "pyinstaller"),
            str(PACKAGING / "opencoord.spec"),
        ]
    )


def linux_tarball(version: str, arch: str) -> Path:
    out = DIST / f"OpenCoord-{version}-linux-{arch}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(ONEDIR, arcname="OpenCoord")
        linux = PACKAGING / "linux"
        tar.add(linux / "99-opencoord-rfexplorer.rules", "OpenCoord/99-opencoord-rfexplorer.rules")
        tar.add(linux / "opencoord.desktop", "OpenCoord/opencoord.desktop")
        tar.add(PACKAGING / "icons" / "opencoord-256.png", "OpenCoord/opencoord.png")
    return out


def appimage(version: str, arch: str) -> Path:
    run(["bash", str(PACKAGING / "linux" / "make-appimage.sh"), str(ONEDIR), version, str(DIST)])
    return DIST / f"OpenCoord-{version}-{arch}.AppImage"


def windows_zip(version: str) -> Path:
    out = DIST / f"OpenCoord-{version}-win64-portable.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ONEDIR.rglob("*")):
            zf.write(path, Path("OpenCoord") / path.relative_to(ONEDIR))
    return out


def find_iscc() -> str:
    found = shutil.which("iscc")
    if found:
        return found
    # Windows environment variable names are case-insensitive.
    for base in filter(None, (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"))):
        for major in ("6", "7"):
            candidate = Path(base) / f"Inno Setup {major}" / "ISCC.exe"
            if candidate.is_file():
                return str(candidate)
    sys.exit("error: Inno Setup compiler (ISCC.exe) not found; run `choco install innosetup`")


def windows_installer(version: str) -> Path:
    run(
        [
            find_iscc(),
            f"/DAppVersion={version}",
            f"/DBundleDir={ONEDIR}",
            f"/DOutputDir={DIST}",
            str(PACKAGING / "windows" / "opencoord.iss"),
        ]
    )
    return DIST / f"OpenCoord-{version}-win64-setup.exe"


def macos_dmg(version: str) -> Path:
    arch = "arm64" if machine_arch() == "aarch64" else machine_arch()
    out = DIST / f"OpenCoord-{version}-macos-{arch}.dmg"
    run(["bash", str(PACKAGING / "macos" / "make-dmg.sh"), str(APP), str(out)])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    step = parser.add_mutually_exclusive_group()
    step.add_argument("--appimage", action="store_true", help="Linux: .tar.gz + .AppImage")
    step.add_argument("--installer", action="store_true", help="Windows: .zip + Inno Setup .exe")
    step.add_argument("--dmg", action="store_true", help="macOS: .dmg")
    args = parser.parse_args()

    wanted = {"--appimage": "linux", "--installer": "win32", "--dmg": "darwin"}
    for flag, plat in wanted.items():
        if getattr(args, flag.lstrip("-")) and not sys.platform.startswith(plat):
            parser.error(f"{flag} must be run on {plat}, not {sys.platform}")

    version = project_version()
    arch = machine_arch()
    DIST.mkdir(exist_ok=True)
    pyinstaller()

    artifacts = [APP if sys.platform == "darwin" else ONEDIR]
    if args.appimage:
        artifacts += [linux_tarball(version, arch), appimage(version, arch)]
    elif args.installer:
        artifacts += [windows_zip(version), windows_installer(version)]
    elif args.dmg:
        artifacts.append(macos_dmg(version))

    print("\nArtifacts:")
    for path in artifacts:
        if not path.exists():
            sys.exit(f"error: expected artifact missing: {path}")
        print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
