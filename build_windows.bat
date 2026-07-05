@echo off
:: ═══════════════════════════════════════════════════════════════════════════
:: NetInspect Pro — Windows Build Script
:: Run this on a Windows machine to create WiFiSurveyPro-Setup.exe
:: Requirements: Python 3.10+  (python.org installer, add to PATH)
:: ═══════════════════════════════════════════════════════════════════════════
setlocal enabledelayedexpansion
set APP_NAME=NetInspect Pro
set DIST_DIR=dist_win

echo.
echo =========================================================
echo  Building %APP_NAME% for Windows
echo =========================================================
echo.

:: ── Check Python ─────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo Found: %%i

:: ── Create / activate venv ────────────────────────────────────────────────
echo.
echo [1/5] Setting up virtual environment...
if not exist ".venv_win" (
    python -m venv .venv_win
)
call .venv_win\Scripts\activate.bat

:: ── Install dependencies ──────────────────────────────────────────────────
echo.
echo [2/5] Installing dependencies...
pip install --quiet --upgrade pip
pip install --quiet ^
    flask ^
    Pillow ^
    numpy ^
    scipy ^
    reportlab ^
    ping3 ^
    dnspython ^
    mac-vendor-lookup ^
    pywebview ^
    pyinstaller

:: ── Clean old build ───────────────────────────────────────────────────────
echo.
echo [3/5] Cleaning previous build...
if exist build_win   rmdir /s /q build_win
if exist %DIST_DIR%  rmdir /s /q %DIST_DIR%

:: ── PyInstaller ───────────────────────────────────────────────────────────
echo.
echo [4/5] Building EXE with PyInstaller...

pyinstaller ^
  --name "WiFi Survey Pro" ^
  --onedir ^
  --windowed ^
  --distpath "%DIST_DIR%" ^
  --workpath build_win ^
  --noconfirm ^
  --add-data "frontend;frontend" ^
  --add-data "scanner_windows.py;." ^
  --hidden-import flask ^
  --hidden-import flask.json ^
  --hidden-import werkzeug ^
  --hidden-import jinja2 ^
  --hidden-import click ^
  --hidden-import itsdangerous ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageFont ^
  --hidden-import numpy ^
  --hidden-import scipy ^
  --hidden-import scipy.spatial ^
  --hidden-import scipy.interpolate ^
  --hidden-import reportlab ^
  --hidden-import dns ^
  --hidden-import dns.resolver ^
  --hidden-import ping3 ^
  --hidden-import mac_vendor_lookup ^
  --hidden-import webview ^
  --hidden-import webview.platforms.winforms ^
  --collect-all webview ^
  --collect-all flask ^
  --collect-all reportlab ^
  --collect-all mac_vendor_lookup ^
  main.py

if errorlevel 1 (
    echo ERROR: PyInstaller build failed!
    pause & exit /b 1
)

:: ── Package as ZIP (no NSIS needed) ──────────────────────────────────────
echo.
echo [5/5] Creating ZIP package...
set ZIP_OUT=%DIST_DIR%\WiFiSurveyPro-Windows.zip

powershell -Command ^
  "Compress-Archive -Path '%DIST_DIR%\WiFi Survey Pro\*' -DestinationPath '%ZIP_OUT%' -Force"

echo.
echo =========================================================
echo  Build complete!
echo  EXE folder : %DIST_DIR%\WiFi Survey Pro\
echo  ZIP package: %ZIP_OUT%
echo =========================================================
echo.
echo 執行方式：解壓縮 ZIP，雙擊 "WiFi Survey Pro.exe"
echo.
pause
