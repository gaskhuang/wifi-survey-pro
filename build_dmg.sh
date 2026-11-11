#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# NetInspect Pro — Build macOS .app + DMG
#
# 策略：內嵌「可攜式 CPython」（python-build-standalone，官方 astral-sh 發行版），
#       依賴裝進它自己的 site-packages。整個 App 不引用任何外部路徑，
#       拿到沒裝 Homebrew / Python 的 Mac 也能直接執行。
#       （舊做法 `venv --copies` 只複製 52 KB 啟動器，stdlib 與 libpython 仍指向
#        建置機的 Homebrew，別台 Mac 一開就閃退。）
#
# 需求：macOS（Apple Silicon）、curl、shasum。產物只支援 Apple Silicon。
# Usage:   ./build_dmg.sh
# Output:  dist/NetInspect Pro.dmg
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_NAME="NetInspect Pro"
BUNDLE_ID="tw.zonetech.netinspect-pro"
VERSION="3.2.0"

# ── 可攜式 Python（版本與 SHA256 鎖定；升級時三個值一起改）──────────────────
PBS_TAG="20261026"
PY_FULL="3.13.18"
PBS_SHA256="8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405"
PBS_FILE="cpython-${PY_FULL}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_FULL}%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SRC_DIR/dist"
CACHE_DIR="$SRC_DIR/build/pbs-cache"          # build/ 已在 .gitignore
APP_BUNDLE="$DIST_DIR/${APP_NAME}.app"
DMG_OUT="$DIST_DIR/${APP_NAME}.dmg"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GRN}▶ $*${NC}"; }
warn()  { echo -e "${YLW}⚠  $*${NC}"; }
error() { echo -e "${RED}✗  $*${NC}"; exit 1; }

# ── 0. Checks ───────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || error "Must run on macOS"
[[ "$(uname -m)" == "arm64" ]] || error "需在 Apple Silicon 上建置（產物為 arm64）"
for c in curl shasum tar codesign hdiutil otool vtool clang; do
    command -v "$c" >/dev/null || error "缺少指令：$c"
done

# ── 1. Clean ────────────────────────────────────────────────────────────────
info "Cleaning dist/…"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR" "$CACHE_DIR"

CONTENTS="$APP_BUNDLE/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
APP_RES="$RESOURCES/app"
PYROOT="$RESOURCES/python"
BPY="$PYROOT/bin/python3"
mkdir -p "$MACOS" "$RESOURCES" "$APP_RES"

# ── 2. 可攜式 Python：下載（有快取就沿用）→ 驗證 SHA256 → 解壓進 bundle ────────
if [[ ! -f "$CACHE_DIR/$PBS_FILE" ]]; then
    info "Downloading CPython $PY_FULL (python-build-standalone $PBS_TAG)…"
    curl -fsSL --retry 3 -o "$CACHE_DIR/$PBS_FILE.part" "$PBS_URL"
    mv "$CACHE_DIR/$PBS_FILE.part" "$CACHE_DIR/$PBS_FILE"
fi
echo "$PBS_SHA256  $CACHE_DIR/$PBS_FILE" | shasum -a 256 -c - >/dev/null \
    || { rm -f "$CACHE_DIR/$PBS_FILE"; error "SHA256 不符（已刪除快取），請重新執行"; }
info "CPython $PY_FULL 驗證通過，解壓中…"
TMPX=$(mktemp -d)
tar -xzf "$CACHE_DIR/$PBS_FILE" -C "$TMPX"
mv "$TMPX/python" "$PYROOT"
rm -rf "$TMPX"
"$BPY" -c "import sys; assert sys.version_info[:2] == (3, 13)" || error "內嵌 Python 無法執行"

# ── 2b. 原生啟動程式：在同一程序內嵌 Python，主執行檔才是 App 本身 ─────────────────
#  （shell 腳本 exec python 會讓 macOS 認不得 App 身分 → 定位授權視窗不會出現，SSID/BSSID 被遮蔽）
#  要在瘦身刪掉 include/ 之前編譯
info "Compiling native launcher…"
LAUNCHER="$MACOS/${APP_NAME}"
clang -O2 -arch arm64 -mmacosx-version-min=14.0 -Wall -Werror \
    -I"$PYROOT/include/python3.13" "$SRC_DIR/launcher/launcher.c" \
    -L"$PYROOT/lib" -lpython3.13 -Wl,-rpath,@executable_path/../Resources/python/lib \
    -o "$LAUNCHER" || error "啟動程式編譯失敗（需要 Xcode Command Line Tools：xcode-select --install）"

# ── 3. 原始碼 + 前端 + 資料 ──────────────────────────────────────────────────
# 一律帶上全部 .py（舊版手寫清單曾漏掉模組，導致打包後一開就崩）；setup.py 只供開發用
info "Copying source files…"
for f in "$SRC_DIR"/*.py; do
    [[ "$(basename "$f")" == "setup.py" ]] && continue
    cp "$f" "$APP_RES/"
done
cp -R "$SRC_DIR/frontend" "$APP_RES/frontend"
[[ -d "$SRC_DIR/data" ]] && cp -R "$SRC_DIR/data" "$APP_RES/data"
[[ -f "$APP_RES/data/oui.txt" ]] || error "缺少 data/oui.txt（離線廠牌資料庫）"

# ── 4. 安裝依賴（只用預先編好的 wheel、版本完全鎖定）────────────────────────────
info "Installing dependencies…"
# proxy_tools（pywebview 相依）只有純 Python 原始碼套件，僅對它允許原始碼安裝
"$BPY" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --only-binary=:all: --no-binary=proxy_tools --no-compile -r "$SRC_DIR/requirements.txt"

# ── 5. 瘦身：拿掉執行期用不到的東西 ─────────────────────────────────────────────
info "Trimming bundle…"
SP="$PYROOT/lib/python3.13/site-packages"
STD="$PYROOT/lib/python3.13"
"$BPY" -m pip uninstall --quiet -y pip >/dev/null 2>&1 || true
rm -rf "$SP"/pip "$SP"/pip-*.dist-info "$SP"/PyObjCTest
find "$SP" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
rm -rf "$STD/test" "$STD/idlelib" "$STD/tkinter" "$STD/turtledemo" "$STD/ensurepip" \
       "$STD/turtle.py" "$STD/config-3.13-darwin" \
       "$STD"/lib-dynload/_tkinter*.so "$STD"/lib-dynload/_test*.so \
       "$PYROOT"/lib/tcl* "$PYROOT"/lib/tk* "$PYROOT"/lib/itcl* "$PYROOT"/lib/thread* \
       "$PYROOT"/lib/libtcl* "$PYROOT"/lib/libtk* "$PYROOT"/lib/pkgconfig \
       "$PYROOT/include" "$PYROOT/share"
find "$PYROOT/bin" -mindepth 1 ! -name python3 ! -name python3.13 -exec rm -rf {} +
find "$RESOURCES" -type d -name __pycache__ -prune -exec rm -rf {} +

# ── 6. 預先編譯 .pyc（執行期以 -B 禁止寫入 bundle，避免每次啟動重新編譯）──────────
info "Precompiling bytecode…"
"$BPY" -m compileall -q -j 0 "$APP_RES" "$STD" >/dev/null || warn "部分檔案編譯失敗（不影響執行）"

# ── 7. App 圖示（啟動程式已在 2b 編譯）────────────────────────────────────────
if [[ -f "$SRC_DIR/AppIcon.icns" ]]; then
    cp "$SRC_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
fi

# ── 8. 驗證：不得引用 bundle 外的函式庫（Homebrew / 使用者目錄 / 其他 Python）────
info "Verifying bundle is self-contained…"
BAD=$(find "$APP_BUNDLE" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) -print0 \
      | xargs -0 file 2>/dev/null | grep "Mach-O" | cut -d: -f1 \
      | while read -r bin; do
            # 只看相依行（以空白/tab 開頭）；多架構檔的「路徑 (architecture …):」標題行要排除
            otool -L "$bin" 2>/dev/null \
              | { grep -E "^[[:space:]]+(/opt/homebrew|/usr/local/|/Users/|/Library/Frameworks/Python)" || true; } \
              | sed "s|^|$bin: |"
        done) || true
[[ -z "$BAD" ]] || { echo "$BAD" | head -20; error "bundle 引用了外部函式庫，無法在其他 Mac 執行"; }

# 最低系統版本：取所有 Mach-O 的最大 minos（numpy 等 wheel 可能要求較新版本）
MIN_OS=$(find "$APP_BUNDLE" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) -print0 \
         | xargs -0 file 2>/dev/null | grep "Mach-O" | cut -d: -f1 \
         | while read -r bin; do vtool -show-build "$bin" 2>/dev/null | awk '/minos/{print $2}' || true; done \
         | sort -t. -k1,1n -k2,2n | tail -1) || true
MIN_OS="${MIN_OS:-14.0}"
info "最低系統版本：macOS ${MIN_OS}（Apple Silicon）"

# 乾淨環境實際 import 全部模組（模擬沒有 Homebrew/開發環境的機器）
env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$BPY" -B -E -s -c "
import sys, os
sys.path.insert(0, '$APP_RES')
assert sys.prefix.startswith('$PYROOT'), sys.prefix
import server, scanner, stability, lan_scanner, traffic_monitor, net_diag, iperf3_mgr
import reporter, recommender, recommendations, analyzer, ai_advisor, heatmap_gen
import speedtest_cf, config, main, webview, numpy, PIL, reportlab, dns.resolver, CoreWLAN
import location_auth, CoreLocation
import device_probe, net_env
heatmap_gen.add_measurement(0.2,0.2,-50,30,'',80,[]); heatmap_gen.add_measurement(0.8,0.7,-75,10,'',30,[])
assert heatmap_gen.generate_heatmap(400,300,True,'rssi',False), 'heatmap 失敗'
assert reporter.generate_diagnosis_pdf({}, {})[:5] == b'%PDF-', 'PDF 失敗'
bad = [m for m in ('scipy','mac_vendor_lookup','aiohttp') if m in sys.modules]
assert not bad, f'不應載入：{bad}'
print('  smoke test OK — Python', sys.version.split()[0])
" || error "bundle 自我測試失敗"

# ── 9. Info.plist ───────────────────────────────────────────────────────────
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
  <key>LSMinimumSystemVersion</key>    <string>${MIN_OS}</string>
  <key>LSArchitecturePriority</key>    <array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSRequiresAquaSystemAppearance</key><false/>
  <key>NSLocationWhenInUseUsageDescription</key>
    <string>NetInspect Pro 需要定位服務以顯示 Wi-Fi 的 SSID 和 BSSID 資訊。</string>
  <key>NSLocationAlwaysUsageDescription</key>
    <string>NetInspect Pro 需要定位服務以顯示 Wi-Fi 的 SSID 和 BSSID 資訊。</string>
  <key>NSLocalNetworkUsageDescription</key>
    <string>NetInspect Pro 需要存取區域網路，才能盤點網段內的設備並取得名稱與型號。</string>
</dict>
</plist>
PLIST

# ── 10. Ad-hoc code sign（失敗要看得到，不再吞掉錯誤）─────────────────────────
info "Code signing (ad-hoc)…"
codesign --force --deep --sign - "$APP_BUNDLE"
codesign --verify --deep --strict "$APP_BUNDLE" && info "Signed ✓"

# 實際透過原生啟動程式跑一次：確認內嵌 Python 正確、App 身分讀得到定位用途說明
info "Launcher self-test…"
SELF=$(env -i HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin" "$LAUNCHER" --selftest) \
    || error "原生啟動程式無法執行"
echo "  $SELF"
echo "$SELF" | "$BPY" -B -E -s -c "
import json, sys
d = json.load(sys.stdin)
assert d['bundle'].endswith('.app'), 'mainBundle 不是 .app：' + d['bundle']
assert d['prefix'].endswith('/Contents/Resources/python'), 'prefix 錯誤：' + d['prefix']
assert d['location'].get('app_bundle'), 'Info.plist 的定位用途說明讀不到'
" || error "啟動程式自我測試失敗"
codesign --verify --deep --strict "$APP_BUNDLE" || error "自我測試後簽章失效（有檔案被寫入 bundle）"

# ── 11. Create DMG（lzma 壓縮，macOS 10.15+ 皆可開）───────────────────────────
info "Creating DMG…"
STAGING=$(mktemp -d)
RW_DMG="$(mktemp -d)/rw.dmg"
trap 'rm -rf "$STAGING" "$(dirname "$RW_DMG")"' EXIT

cp -R "$APP_BUNDLE" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
APP_MB=$(du -sm "$APP_BUNDLE" | cut -f1)

hdiutil create -srcfolder "$STAGING" -volname "$APP_NAME" -fs HFS+ \
    -format UDRW -size "$(( APP_MB + 30 ))m" "$RW_DMG" > /dev/null

MOUNT_PT=$(mktemp -d)
hdiutil attach "$RW_DMG" -mountpoint "$MOUNT_PT" -nobrowse -quiet
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
hdiutil convert "$RW_DMG" -format ULMO -o "$DMG_OUT" > /dev/null

# ── 12. Done ────────────────────────────────────────────────────────────────
DMG_SZ=$(du -sh "$DMG_OUT" | cut -f1)
APP_SZ=$(du -sh "$APP_BUNDLE" | cut -f1)
echo ""
echo -e "${GRN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GRN}  ✓ Build complete!  v${VERSION}  (Python ${PY_FULL}, macOS ${MIN_OS}+, Apple Silicon)${NC}"
echo -e "${GRN}  App : $APP_BUNDLE  (${APP_SZ})${NC}"
echo -e "${GRN}  DMG : $DMG_OUT  (${DMG_SZ})${NC}"
echo -e "${GRN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "安裝方式："
echo "  1. 打開 DMG，將「${APP_NAME}」拖到 Applications"
echo "  2. 首次開啟若被 macOS 擋下：系統設定 › 隱私權與安全性 › 往下捲 › 「仍要打開」"
echo "     （或在終端機執行：xattr -dr com.apple.quarantine \"/Applications/${APP_NAME}.app\"）"
echo ""
