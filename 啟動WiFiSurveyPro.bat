@echo off
chcp 65001 >nul
title Wi-Fi Survey Pro

echo.
echo  ╔══════════════════════════════════════╗
echo  ║       Wi-Fi Survey Pro 啟動中...     ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── 設定資料夾路徑 ────────────────────────────────────────────────────────
set APP_DIR=%USERPROFILE%\WiFiSurveyPro
set REPO_URL=https://github.com/gaskhuang/wifi-survey-pro.git

:: ── 檢查 Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python！
    echo.
    echo 請先安裝 Python 3.10 以上版本：
    echo https://www.python.org/downloads/
    echo.
    echo 安裝時請勾選 "Add Python to PATH"
    pause
    start https://www.python.org/downloads/
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [OK] %%v

:: ── 檢查 Git ─────────────────────────────────────────────────────────────
git --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Git！
    echo.
    echo 請先安裝 Git：
    echo https://git-scm.com/download/win
    pause
    start https://git-scm.com/download/win
    exit /b 1
)
for /f "tokens=*" %%v in ('git --version 2^>^&1') do echo [OK] %%v

echo.

:: ── 下載或更新程式 ────────────────────────────────────────────────────────
if exist "%APP_DIR%\.git" (
    echo [1/3] 更新程式到最新版本...
    cd /d "%APP_DIR%"
    git pull --quiet
    if errorlevel 1 (
        echo [警告] 更新失敗，使用現有版本繼續...
    ) else (
        echo [OK] 已更新
    )
) else (
    echo [1/3] 下載程式中（首次需要一點時間）...
    git clone "%REPO_URL%" "%APP_DIR%"
    if errorlevel 1 (
        echo.
        echo [錯誤] 下載失敗，請確認網路連線後重試。
        pause
        exit /b 1
    )
    cd /d "%APP_DIR%"
    echo [OK] 下載完成
)

:: ── 建立 Python 虛擬環境 ──────────────────────────────────────────────────
echo.
if exist "%APP_DIR%\.venv\Scripts\activate.bat" (
    echo [2/3] 虛擬環境已存在，略過建立...
) else (
    echo [2/3] 建立 Python 虛擬環境...
    python -m venv "%APP_DIR%\.venv"
    if errorlevel 1 (
        echo [錯誤] 虛擬環境建立失敗！
        pause
        exit /b 1
    )
    echo [OK] 虛擬環境建立完成
)

:: ── 安裝依賴套件 ──────────────────────────────────────────────────────────
echo.
echo [3/3] 安裝 / 更新依賴套件...
call "%APP_DIR%\.venv\Scripts\activate.bat"
pip install --quiet --upgrade pip
pip install --quiet -r "%APP_DIR%\requirements_windows.txt"
if errorlevel 1 (
    echo [錯誤] 套件安裝失敗！
    pause
    exit /b 1
)
echo [OK] 套件安裝完成

:: ── 啟動 ─────────────────────────────────────────────────────────────────
echo.
echo ════════════════════════════════════════
echo  Wi-Fi Survey Pro 啟動中...
echo  瀏覽器將自動開啟 http://127.0.0.1:5173
echo  關閉此視窗可停止程式
echo ════════════════════════════════════════
echo.

cd /d "%APP_DIR%"
python main.py

:: 若程式異常退出，暫停讓使用者看到錯誤訊息
echo.
echo [程式已停止]
pause
