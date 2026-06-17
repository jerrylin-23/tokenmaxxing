#!/usr/bin/env bash
#
# Packages Tokenmaxxing.app into a ready-to-run DMG installer.
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$REPO_DIR/dist"
APP_PATH="$DIST_DIR/Tokenmaxxing.app"
DMG_ROOT="$DIST_DIR/dmg_root"
DMG_PATH="$DIST_DIR/Tokenmaxxing.dmg"

echo "=== Packaging Tokenmaxxing.app into DMG ==="

# 1. Ensure the app bundle exists
if [ ! -d "$APP_PATH" ]; then
    echo "[Error] App bundle not found at: $APP_PATH" >&2
    echo "Please build the app first using PyInstaller." >&2
    exit 1
fi

# 2. Clean up old build artifacts
rm -rf "$DMG_ROOT"
rm -f "$DMG_PATH"

# 3. Create structure for DMG
mkdir -p "$DMG_ROOT"
echo "Copying Tokenmaxxing.app to installer root..."
cp -R "$APP_PATH" "$DMG_ROOT/"

echo "Creating Applications folder shortcut..."
ln -s /Applications "$DMG_ROOT/Applications"

# 4. Create DMG
echo "Generating DMG installer..."
hdiutil create \
    -volname "Tokenmaxxing" \
    -srcfolder "$DMG_ROOT" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

# 5. Clean up temp folder
rm -rf "$DMG_ROOT"

echo "=========================================="
echo "Success! DMG created at:"
echo "  $DMG_PATH"
echo "=========================================="
