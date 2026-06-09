# Wi-Fi Survey Pro

macOS / Windows Wi-Fi 現場勘測工具，提供即時頻譜視覺化、頻道分析、訊號熱力圖、效能測試與 PDF 報告輸出。

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 功能

| 功能 | 說明 |
|------|------|
| **即時掃描** | 列出所有可見 AP：SSID、BSSID、廠商、訊號、頻道、頻寬、安全性 |
| **頻譜視覺化** | Bell curve 波形圖，每個 AP 獨立顏色，支援 2.4 / 5 / 6 GHz |
| **Freeze / Filter** | 暫停即時更新、SSID/BSSID 過濾、hover tooltip |
| **頻道分析** | 同頻干擾分析，自動推薦最佳頻道（2.4 / 5 / 6 GHz） |
| **訊號熱力圖** | 上傳平面圖，點選量測點，生成 RBF 插值熱力圖 |
| **效能測試** | Ping 延遲、Jitter、封包遺失、DNS 解析時間 |
| **PDF 報告** | 含中文、頻譜截圖、熱力圖、頻道建議、企業標準參考表 |

---

## 下載安裝

### macOS（推薦）

1. 前往 [Releases](https://github.com/gaskhuang/wifi-survey-pro/releases/latest) 下載 `Wi-Fi Survey Pro.dmg`
2. 打開 DMG，將 App 拖到 **Applications**
3. 首次開啟請右鍵 > **開啟**（繞過 Gatekeeper）

   或執行：
   ```bash
   xattr -d com.apple.quarantine "/Applications/Wi-Fi Survey Pro.app"
   ```

4. 開啟 App 後，前往**系統設定 → 隱私權與安全性 → 定位服務**，開啟 Terminal 的定位權限以顯示 SSID / BSSID

### Windows（測試執行）

```powershell
git clone https://github.com/gaskhuang/wifi-survey-pro.git
cd wifi-survey-pro

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements_windows.txt

python main.py
```

瀏覽器會自動開啟 `http://127.0.0.1:5173`

---

## 從原始碼執行（macOS 開發）

```bash
git clone https://github.com/gaskhuang/wifi-survey-pro.git
cd wifi-survey-pro

./install.sh     # 建立 venv 並安裝所有依賴
./run.sh         # 啟動
```

或手動：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

預設開啟 `http://127.0.0.1:5173`

---

## 打包

### macOS DMG

```bash
./build_dmg.sh
# 輸出：dist/Wi-Fi Survey Pro.dmg（約 60 MB）
```

### Windows EXE

在 Windows 機器上執行：

```bat
build_windows.bat
:: 輸出：dist_win/WiFiSurveyPro-Windows.zip
```

### GitHub Actions（自動建置）

Push 到 `master` 或建立 `v*` tag 時，CI 會自動在 macOS + Windows runner 上建置並上傳 artifact。建立 tag 會同時發佈 GitHub Release。

```bash
git tag v2.1.1 && git push origin v2.1.1
```

---

## 技術架構

```
Wi-Fi Survey Pro
├── main.py              # 入口：啟動 Flask + pywebview
├── server.py            # Flask REST API
├── scanner.py           # macOS CoreWLAN 掃描 + 跨平台 dispatch
├── scanner_windows.py   # Windows netsh wlan 掃描後端
├── analyzer.py          # 頻道干擾分析
├── heatmap_gen.py        # 熱力圖生成（RBF 插值）
├── reporter.py          # PDF 報告（ReportLab + STHeiti 中文字體）
├── recommender.py       # 優化建議引擎
├── iperf3_mgr.py        # iperf3 吞吐量測試管理
└── frontend/
    ├── index.html
    ├── css/style.css
    └── js/
        ├── scanner.js   # 掃描表格、排序、過濾
        ├── spectrum.js  # Canvas 頻譜圖（Bell curve、Bézier）
        ├── heatmap.js   # 熱力圖互動
        ├── channels.js  # 頻道分析頁面
        ├── netperf.js   # 效能測試
        └── report.js    # PDF 報告產生
```

**後端依賴**

| 套件 | 用途 |
|------|------|
| `pyobjc-framework-CoreWLAN` | macOS Wi-Fi 掃描（僅 macOS） |
| `flask` | REST API server |
| `pywebview` | 原生視窗（WebKit） |
| `Pillow` + `numpy` + `scipy` | 熱力圖 RBF 插值 |
| `reportlab` | PDF 生成 |
| `mac-vendor-lookup` | BSSID → 廠商名稱 |
| `ping3` + `dnspython` | 網路效能測試 |

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/scan` | 掃描並回傳所有 AP |
| GET | `/api/current` | 目前連線資訊 |
| POST | `/api/analyze` | 頻道干擾分析 |
| POST | `/api/heatmap/point` | 新增熱力圖量測點 |
| GET | `/api/heatmap/generate` | 生成熱力圖（PNG base64） |
| POST | `/api/report` | 產生 PDF 報告 |
| GET | `/api/netperf/ping` | Ping 測試 |
| GET | `/api/netperf/dns` | DNS 解析測試 |
| GET | `/api/bands` | 支援的頻段 |

---

## 系統需求

| 平台 | 需求 |
|------|------|
| macOS | 12.0+，Python 3.10+，CoreWLAN（內建） |
| Windows | 10/11，Python 3.10+，無線網卡 |

---

## License

MIT © 2026 [gaskhuang](https://github.com/gaskhuang)
