#!/bin/sh
# Build "Bridge Commander.app" (macOS remote client) and optionally install its LaunchAgent.
#   packaging/macos/build_app.sh [dest-dir] [--install|--release]
# The app wraps dist/bridge-commander.pyz; give the app Full Disk Access (or the folders you share)
# in System Settings → Privacy & Security, because launchd-started processes get no file permissions.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
dest=${1:-"$HOME/Applications"}
app="$dest/Bridge Commander.app"
mode=${2:-}
case "$mode" in ""|--install|--release) ;; *) echo "unknown mode: $mode" >&2; exit 2;; esac
identity=${BC_SIGN_IDENTITY:--}
if [ "$mode" = "--release" ]; then
    case "$identity" in "Developer ID Application: "*) ;; *) echo "release requires BC_SIGN_IDENTITY=Developer ID Application: ..." >&2; exit 2;; esac
    [ -n "${BC_NOTARY_PROFILE:-}" ] || { echo "release requires an existing BC_NOTARY_PROFILE in Keychain" >&2; exit 2; }
    [ ! -e "$app" ] || { echo "release destination already exists; choose a fresh artifact directory" >&2; exit 2; }
fi
version=$(python3 -I "$root/dist/bridge-commander.pyz" version 2>/dev/null | awk '{print $NF}')
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cc -O2 -Wall -Wextra -arch arm64 -arch x86_64 -o "$app/Contents/MacOS/bridge-commander-remote" "$here/launcher.c"
cp "$root/dist/bridge-commander.pyz" "$app/Contents/Resources/bridge-commander.pyz"
sed "s/VERSION/$version/g" "$here/Info.plist" > "$app/Contents/Info.plist"
if [ "$mode" = "--release" ]; then
    codesign --force --sign "$identity" --identifier dev.bridgecommander.remote --options runtime --timestamp "$app"
    codesign --verify --deep --strict "$app"
    ditto -c -k --keepParent "$app" "$dest/Bridge Commander-notarization.zip"
    xcrun notarytool submit "$dest/Bridge Commander-notarization.zip" --keychain-profile "$BC_NOTARY_PROFILE" --wait --output-format json > "$dest/notarization.json"
    python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("status")=="Accepted" else 1)' "$dest/notarization.json"
    xcrun stapler staple "$app"
    spctl --assess --type execute --verbose=2 "$app"
    ditto -c -k --keepParent "$app" "$dest/Bridge Commander.zip"
    shasum -a 256 "$dest/Bridge Commander.zip" > "$dest/SHA256SUMS"
else
    codesign --force --sign - --identifier dev.bridgecommander.remote "$app"
fi
echo "built $app ($version)"
if [ "${2:-}" = "--install" ]; then
    agent="$HOME/Library/LaunchAgents/dev.bridgecommander.remote.plist"
    launchctl bootout "gui/$(id -u)/dev.bridgecommander.remote" 2>/dev/null || true
    sed -e "s#APP#$app#" -e "s#LOG#$HOME/Library/Logs/bridge-commander-remote.log#g" \
        "$here/dev.bridgecommander.remote.plist" > "$agent"
    plutil -lint "$agent" >/dev/null
    launchctl bootstrap "gui/$(id -u)" "$agent"
    echo "installed LaunchAgent $agent"
fi
