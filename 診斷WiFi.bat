@echo off
chcp 65001 >nul
title NetInspect Pro 診斷工具

echo ════════════════════════════════════════════
echo  NetInspect Pro — Windows 診斷
echo ════════════════════════════════════════════
echo.

echo [1] WLAN 服務狀態：
sc query WlanSvc | findstr "STATE"
echo.

echo [2] 無線網卡清單：
netsh wlan show interfaces | findstr /i "Name State"
echo.

echo [3] netsh 掃描原始輸出（前 60 行）：
echo ----------------------------------------
netsh wlan show networks mode=bssid 2>&1 | head -60
echo.
echo ─ 若上方沒有網路資訊，改用下列指令 ─
netsh wlan show networks 2>&1 | head -30
echo.

echo [4] Python 版本：
python --version 2>&1
echo.

echo [5] 測試掃描（直接執行 Python）：
cd /d %USERPROFILE%\WiFiSurveyPro
call .venv\Scripts\activate.bat
python -c "import scanner_windows; nets=scanner_windows.scan_networks(); print(f'找到 {len(nets)} 個網路'); [print(f'  {n[\"ssid\"]} ch{n[\"channel\"]} {n[\"signal_pct\"]}%%') for n in nets[:5]]" 2>&1
echo.

echo ════════════════════════════════════════════
echo  請把以上輸出結果截圖或複製給開發者
echo ════════════════════════════════════════════
pause
