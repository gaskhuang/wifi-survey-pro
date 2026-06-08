#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Wi-Fi Survey Pro — 安裝腳本 (macOS)
# ═══════════════════════════════════════════════════════════════════════════
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APP_DIR/.venv"

echo "╔════════════════════════════════════════╗"
echo "║   Wi-Fi Survey Pro — 安裝程式          ║"
echo "╚════════════════════════════════════════╝"
echo ""

# 1. Check Python
echo "▸ 檢查 Python 3.10+ ..."
if ! command -v python3 &>/dev/null; then
  echo "❌ 找不到 python3，請先安裝：brew install python3"
  exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python $PY_VER ✓"

# 2. Create virtual environment
echo "▸ 建立虛擬環境 (.venv) ..."
python3 -m venv "$VENV"
source "$VENV/bin/activate"

# 3. Upgrade pip
echo "▸ 更新 pip ..."
pip install --quiet --upgrade pip

# 4. Install dependencies
echo "▸ 安裝相依套件（這可能需要 1-3 分鐘）..."
pip install --quiet -r "$APP_DIR/requirements.txt"

echo ""
echo "✅ 安裝完成！"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "▸ 啟動方法："
echo "   ./run.sh"
echo ""
echo "▸ 首次執行時，macOS 可能要求："
echo "   • 允許「定位服務」→ 系統設定 → 隱私權 → 定位服務 → 開啟"
echo "   • 允許網路存取"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
