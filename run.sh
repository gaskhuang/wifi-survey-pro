#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Wi-Fi Survey Pro — 啟動腳本
# ═══════════════════════════════════════════════════════════════════════════
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APP_DIR/.venv"

if [ ! -d "$VENV" ]; then
  echo "❌ 尚未安裝，請先執行：./install.sh"
  exit 1
fi

source "$VENV/bin/activate"
echo "Wi-Fi Survey Pro 啟動中..."
cd "$APP_DIR"
python3 main.py "$@"
