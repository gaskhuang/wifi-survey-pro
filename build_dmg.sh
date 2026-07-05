#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# NetInspect Pro — Build macOS .app + DMG
#
# Strategy: embed the venv inside the .app bundle (no PyInstaller/py2app).
# Works on the machine it's built on; same Python version guaranteed.
#
# Usage:   ./build_dmg.sh
# Output:  dist/NetInspect Pro.dmg
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_NAME="NetInspect Pro"
BUNDLE_ID="tw.zonetech.netinspect-pro"
VERSION="3.0.0"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SRC_DIR/dist"
APP_BUNDLE="$DIST_DIR/${APP_NAME}.app"
DMG_OUT="$DIST_DIR/${APP_NAME}.dmg"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}▶ $*${NC}"; }
warn()  { echo -e "${YLW}⚠  $*${NC}"; }
error() { echo -e "${RED}✗  $*${NC}"; exit 1; }

# ── 0. Checks ───────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || error "Must run on macOS"
[[ -d "$SRC_DIR/.venv" ]]    || error ".venv not found — run ./install.sh first"

PYTHON="$SRC_DIR/.venv/bin/python3"
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PY_VER | $(uname -m)"

# ── 1. Clean ────────────────────────────────────────────────────────────────
info "Cleaning dist/…"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# ── 2. Build .app structure ──────────────────────────────────────────────────
info "Building ${APP_NAME}.app…"

CONTENTS="$APP_BUNDLE/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
APP_RES="$RESOURCES/app"

mkdir -p "$MACOS" "$RESOURCES" "$APP_RES"

# ── 3. Copy Python source files ──────────────────────────────────────────────
info "Copying source files…"
SOURCE_FILES=(
    main.py server.py scanner.py analyzer.py
    heatmap_gen.py reporter.py recommender.py recommendations.py
    iperf3_mgr.py stability.py speedtest_cf.py ai_advisor.py
    lan_scanner.py platform_caps.py
    config.py ai_providers.py traffic_monitor.py net_diag.py
)
for f in "${SOURCE_FILES[@]}"; do
    [[ -f "$SRC_DIR/$f" ]] && cp "$SRC_DIR/$f" "$APP_RES/" || warn "Missing: $f"
done

# Copy frontend
info "Copying frontend…"
cp -r "$SRC_DIR/frontend" "$APP_RES/frontend"

# Copy data (bundled OUI database for offline vendor lookup)
# 斷言離線 OUI 資料庫存在且非空，否則現場離線掃描會退回連網下載而卡住
[[ -s "$SRC_DIR/data/oui.txt" ]] || error "data/oui.txt 缺失或為空——離線 OUI 廠牌查詢會失效，中止打包"
info "Copying data (OUI database)…"
cp -r "$SRC_DIR/data" "$APP_RES/data"

# ── 4. Create a relocatable Python venv inside the bundle ────────────────────
info "Creating bundled Python venv (this may take a minute)…"
BUNDLED_VENV="$RESOURCES/venv"

# --copies ensures python binary is copied, not symlinked (makes it relocatable)
"$PYTHON" -m venv --copies "$BUNDLED_VENV"

BPYTHON="$BUNDLED_VENV/bin/python3"

# Install requirements into bundled venv
info "Installing dependencies into bundle venv…"
"$BPYTHON" -m pip install --quiet --upgrade pip
"$BPYTHON" -m pip install --quiet -r "$SRC_DIR/requirements.txt"

# ── 5. Launcher script ───────────────────────────────────────────────────────
info "Writing launcher…"

LAUNCHER="$MACOS/${APP_NAME}"
cat > "$LAUNCHER" << 'BASH'
#!/usr/bin/env bash
# Resolve bundle paths
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCES="$SCRIPT_DIR/../Resources"
PYTHON="$RESOURCES/venv/bin/python3"
APP="$RESOURCES/app/main.py"

# Tell Python where to find app modules
# PYTHONDONTWRITEBYTECODE：避免執行期在 bundle 內寫 .pyc，破壞程式碼簽章封印
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$RESOURCES/app:$PYTHONPATH"

# Launch
exec "$PYTHON" "$APP"
BASH

chmod +x "$LAUNCHER"

# ── 5b. App icon（若專案根目錄有 AppIcon.icns 就帶上）──────────────────────────
if [[ -f "$SRC_DIR/AppIcon.icns" ]]; then
    info "Adding app icon…"
    cp "$SRC_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
fi

# ── 6. Info.plist ────────────────────────────────────────────────────────────
info "Writing Info.plist…"

cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key>       <string>${APP_NAME}</string>
  <key>CFBundleName</key>              <string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key>        <string>${BUNDLE_ID}</string>
  <key>CFBundleExecutable</key>        <string>${APP_NAME}</string>
  <key>CFBundleIconFile</key>          <string>AppIcon</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key>           <string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key>    <string>12.0</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSRequiresAquaSystemAppearance</key><false/>
  <key>NSLocationWhenInUseUsageDescription</key>
    <string>NetInspect Pro 需要定位服務以顯示 SSID 和 BSSID 資訊。</string>
  <key>NSLocationAlwaysUsageDescription</key>
    <string>NetInspect Pro 需要定位服務以顯示 SSID 和 BSSID 資訊。</string>
</dict>
</plist>
PLIST

# ── 7. Ad-hoc code sign (removes Gatekeeper block on same machine) ───────────
info "Code signing (ad-hoc)…"
codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null \
  && info "Signed ✓" \
  || warn "codesign failed — first launch may need right-click > Open"

# ── 8. Create DMG ────────────────────────────────────────────────────────────
info "Creating DMG…"

STAGING=$(mktemp -d)
trap "rm -rf $STAGING /tmp/wifi_rw.dmg" EXIT

cp -r "$APP_BUNDLE" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

APP_MB=$(du -sm "$APP_BUNDLE" | cut -f1)
DMG_MB=$(( APP_MB + 30 ))

hdiutil create \
    -srcfolder  "$STAGING" \
    -volname    "$APP_NAME" \
    -fs         HFS+ \
    -format     UDRW \
    -size       "${DMG_MB}m" \
    /tmp/wifi_rw.dmg \
    > /dev/null

# Optional: set Finder window layout
MOUNT_PT=$(mktemp -d)
hdiutil attach /tmp/wifi_rw.dmg -mountpoint "$MOUNT_PT" -nobrowse -quiet
osascript - "$APP_NAME" "$MOUNT_PT" <<'AS' 2>/dev/null || true
on run {vol, mp}
  tell application "Finder"
    set d to disk vol
    tell container window of d
      set toolbar visible to false; set statusbar visible to false
      set current view to icon view
      set bounds to {200, 140, 760, 420}
    end tell
    set opts to icon view options of container window of d
    tell opts; set icon size to 96; set arrangement to not arranged; end tell
    set position of item (vol & ".app") of d to {160, 175}
    set position of item "Applications" of d to {400, 175}
    update d; delay 1; close container window of d
  end tell
end run
AS
hdiutil detach "$MOUNT_PT" -quiet
rm -rf "$MOUNT_PT"

hdiutil convert /tmp/wifi_rw.dmg \
    -format UDZO -imagekey zlib-level=9 \
    -o "$DMG_OUT" > /dev/null

# ── 9. Done ──────────────────────────────────────────────────────────────────
DMG_SZ=$(du -sh "$DMG_OUT" | cut -f1)
APP_SZ=$(du -sh "$APP_BUNDLE" | cut -f1)

echo ""
echo -e "${GRN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GRN}  ✓ Build complete!${NC}"
echo -e "${GRN}  App : $APP_BUNDLE  (${APP_SZ})${NC}"
echo -e "${GRN}  DMG : $DMG_OUT  (${DMG_SZ})${NC}"
echo -e "${GRN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "安裝方式："
echo "  1. 打開 DMG 檔"
echo "  2. 將「${APP_NAME}」拖到 Applications 資料夾"
echo "  3. 首次開啟請右鍵 > 開啟（Gatekeeper 繞過）"
echo "     或執行：xattr -d com.apple.quarantine \"/Applications/${APP_NAME}.app\""
echo ""

open "$DIST_DIR" 2>/dev/null || true
