#!/usr/bin/env bash
# Ad-hoc sign OpenCoord.app and wrap it in a compressed DMG with an /Applications symlink.
#
#   packaging/macos/make-dmg.sh <path/to/OpenCoord.app> <output.dmg>
#
# Ad-hoc signing (`-s -`) is not notarisation: Gatekeeper still asks on first launch
# (right-click > Open). Notarisation needs an Apple Developer ID and is optional.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <OpenCoord.app> <output.dmg>" >&2
    exit 2
fi

app=${1%/}
dmg=$2
[[ -d "$app" ]] || { echo "error: $app not found" >&2; exit 1; }

codesign --force --deep -s - "$app"
codesign --verify --deep --strict "$app"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"

mkdir -p "$(dirname "$dmg")"
rm -f "$dmg"
hdiutil create -volname OpenCoord -srcfolder "$staging" -fs HFS+ -format UDZO -ov "$dmg"
echo "$dmg"
