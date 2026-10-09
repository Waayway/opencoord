#!/usr/bin/env bash
# Wrap the PyInstaller onedir bundle into an AppImage.
#
#   packaging/linux/make-appimage.sh <onedir> <version> <outdir>
#
# Builds an AppDir (AppRun, .desktop, icon, AppStream metainfo, udev rule), downloads the
# appimagetool continuous release for this machine's architecture (cached in build/), and writes
# <outdir>/OpenCoord-<version>-<arch>.AppImage. appimagetool runs with --appimage-extract-and-run,
# so no FUSE is needed (works in Docker and on CI runners).
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 <onedir> <version> <outdir>" >&2
    exit 2
fi

onedir=$(realpath "$1")
version=$2
outdir=$(realpath -m "$3")
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)
arch=${ARCH:-$(uname -m)}
app_id=io.github.waayway.opencoord

[[ -x "$onedir/OpenCoord" ]] || { echo "error: $onedir/OpenCoord not found" >&2; exit 1; }

work="$root/build/appimage"
appdir="$work/OpenCoord.AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr/lib" \
         "$appdir/usr/share/applications" \
         "$appdir/usr/share/metainfo" \
         "$appdir/usr/share/icons/hicolor/256x256/apps" \
         "$appdir/usr/lib/udev/rules.d"

cp -a "$onedir" "$appdir/usr/lib/opencoord"

cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/opencoord/OpenCoord" "$@"
APPRUN
chmod +x "$appdir/AppRun"

# The desktop-file id must match the AppStream <launchable>.
cp "$here/opencoord.desktop" "$appdir/usr/share/applications/$app_id.desktop"
cp "$here/opencoord.desktop" "$appdir/$app_id.desktop"
# appimagetool only looks for the legacy <id>.appdata.xml name (and validates it if appstreamcli exists).
cp "$here/$app_id.metainfo.xml" "$appdir/usr/share/metainfo/$app_id.appdata.xml"
cp "$root/packaging/icons/opencoord-256.png" "$appdir/usr/share/icons/hicolor/256x256/apps/opencoord.png"
cp "$root/packaging/icons/opencoord-256.png" "$appdir/opencoord.png"
ln -sf opencoord.png "$appdir/.DirIcon"
# Not installed by the AppImage itself; shipped so users can copy it (see README).
cp "$here/99-opencoord-rfexplorer.rules" "$appdir/usr/lib/udev/rules.d/"

tool="$work/appimagetool-$arch.AppImage"
if [[ ! -x "$tool" ]]; then
    url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$arch.AppImage"
    echo "Downloading $url"
    curl -fsSL --retry 3 -o "$tool.part" "$url"
    chmod +x "$tool.part"
    mv "$tool.part" "$tool"
fi

mkdir -p "$outdir"
out="$outdir/OpenCoord-$version-$arch.AppImage"
ARCH="$arch" "$tool" --appimage-extract-and-run "$appdir" "$out"
echo "$out"
