# NetInspect Pro — 現場網路健檢平台 規格書 v0.1

> 由 NetInspect Pro 升級而來。定位從「Wi-Fi 勘測」擴大為
> **IT 系統廠商到府網路健檢工具**：工程師帶一台筆電到客戶現場，
> 一個 App 跑完 Wi-Fi + 有線 LAN + 流量 + AI 診斷，交付一份完整檢測報告。

**狀態**：規劃中，等業主逐項確認後動工。
**本文件目的**：把每個功能、API、UI、限制講清楚，作為實作與驗收依據。

---

## 0. 已定案的方向

| 決策 | 選擇 |
|---|---|
| 程式碼結構 | **統一跨平台單一程式碼庫**（平台判斷，一份 code 打包 mac + Windows） |
| AI 引擎 | **兩者並存**：離線規則引擎為預設（一定能跑），雲端 Claude API 為進階選配 |
| 封包功能 | **輕量流量異常監測**（統計層級：top talkers / 廣播風暴 / loop / 亂發封包；不做完整封包解碼） |
| 推進方式 | 先出本規格書 → 業主逐項確認 → 分階段實作 |
| 上傳 | 每次更新自動 push 到 GitHub（gaskhuang/wifi-survey-pro） |

**待業主決定（本文件標示 ❓ 處）**：產品名稱、白標深度、授權機制、雙語預設語言。

---

## 1. 產品定位與賣點

- **對象**：資訊系統整合商（SI）、網路工程外包、MSP、IT 維運。
- **使用情境**：工程師到客戶現場 → 一鍵健檢 → 產出可交付客戶的專業報告。
- **核心賣點**：
  1. **一個 App 抵一整套工具**（取代 inSSIDer + Angry IP + 部分 Wireshark + iperf3 + ping/arp 指令）。
  2. **AI 輔助判讀**：不論工程師資歷深淺，都能看懂問題在哪、下一步怎麼修。
  3. **可交付的檢測報告**（可放廠商 logo → 廠商用自己品牌交客戶）。
  4. **開箱即用**：離線也能跑，不依賴外部服務。

### 產品名稱 ❓
現名「NetInspect Pro」已不符功能範圍。建議候選（請圈選或另提）：
- **NetInspect Pro**（網路現場健檢）← 建議
- **SiteNet Inspector**
- **NetProbe / 網檢通**

---

## 2. 系統架構

### 2.1 技術棧（沿用現有，跨平台強化）
- 後端：Python + Flask（現有）
- 前端：原生 HTML/CSS/JS 分頁式（現有）
- 桌面外殼：pywebview（mac + Windows 皆支援）
- 打包：mac = 內嵌 venv 的 .app / .dmg（現有 `build_dmg.sh`）；Windows = PyInstaller → Setup.exe（現有 `build_windows.bat`，需驗證）

### 2.2 跨平台策略（單一程式碼庫）
```
platform.py          ← 統一平台偵測與能力探測（is_windows / has_npcap / is_admin…）
scanner.py           ← Wi-Fi 掃描：內部依平台分派 mac / windows 實作
  ├ _scan_macos()    （現有 scanner.py 邏輯）
  └ _scan_windows()  （現有 scanner_windows.py 併入）
```
> 現在 mac/Windows 是兩個檔；統一後由 `scanner.py` 內部依 `platform.py` 分派，
> 上層 API 不需知道平台。**改一次、兩邊生效。**

### 2.3 模組總覽（新增以 🆕 標示）
| 模組 | 檔案 | 職責 |
|---|---|---|
| 平台抽象 🆕 | `platform_caps.py` | OS 偵測、權限檢查、Npcap/libpcap 探測 |
| Wi-Fi 掃描 | `scanner.py` | 現有，併入 Windows |
| 通道分析 | `analyzer.py` | 現有 |
| 穩定度 | `stability.py` | 現有 |
| 熱圖 | `heatmap_gen.py` | 現有 |
| 對外測速 | `speedtest_cf.py` | 現有 |
| 吞吐量 | `iperf3_mgr.py` | 現有（可擴充內網對 NAS） |
| **LAN 盤查** 🆕 | `lan_scanner.py` | 主機探測、設備指紋、資產盤查 |
| **流量監測** 🆕 | `traffic_monitor.py` | top talkers、廣播風暴、loop、IP 衝突、rogue IP |
| **網路診斷** 🆕 | `net_diag.py` | ping/traceroute/dns/arp/route、子網計算 |
| 建議引擎 | `recommendations.py`/`recommender.py` | 現有，擴充 LAN/流量規則 |
| AI 顧問 | `ai_advisor.py` | 現有，擴充分層診斷 + 雲端選配 |
| 報告 | `reporter.py` | 現有，新增整合檢測報告 + 白標 |
| i18n 🆕 | `frontend/i18n/*.json` | 中英雙語 |
| 授權 🆕（選配） | `license.py` | licence key 啟用（P5 再定） |

---

## 3. 新功能規格

### 3.1 LAN 盤查（`lan_scanner.py`）🆕

**目標**：掃出同網段所有活躍設備，判斷數量、品牌、類型、型號，做完整地端資產盤查。

**探測方法（多訊號融合，越多訊號信心越高）**：
| 訊號 | 取得方式 | 得到什麼 |
|---|---|---|
| ARP 掃描 | 同網段 ARP request（需提權） | 存活主機 + MAC |
| ICMP sweep | ping 整個網段 | 存活主機（無提權備援） |
| MAC OUI | 本地 OUI 資料庫（`mac-vendor-lookup`） | **設備品牌**（可靠 ✅） |
| TCP 埠探測 | 連 22/80/443/445/515/631/9100/161… | 服務指紋 → **設備類型** |
| mDNS/Bonjour | `_services._dns-sd` 查詢 | 型號/名稱（Apple、印表機、NAS 常有） |
| NetBIOS/SMB | 137/139/445 名稱查詢 | 主機名、Windows 電腦 |
| SNMP | sysDescr（161，若開放/公開 community） | **精確型號**（網通設備常有） |
| UPnP/SSDP | 1900 廣播 | 型號（路由器、IoT） |

**設備類型判斷（啟發式）**：
- 印表機/掃描器 → 埠 515/631/9100 或 mDNS `_ipp/_scanner`
- NAS → 埠 5000/5001（Synology）、445 + mDNS `_smb`、QNAP 指紋
- 防火牆/路由器 → SNMP sysDescr、UPnP、閘道 IP、廠牌 OUI（Fortinet/Palo/Cisco…）
- Wi-Fi AP/控制器 → OUI（Ubiquiti/Aruba/Ruckus…）+ SNMP
- PC/Mac → NetBIOS（Windows）、mDNS `_device-info`（Apple）、TTL 指紋
- 手機/IoT → OUI + 少量開放埠

**輸出（每台設備）**：
```json
{ "ip": "192.168.1.23", "mac": "a4:2b:8c:...", "vendor": "Synology",
  "type": "NAS", "model": "DS920+", "hostname": "OFFICE-NAS",
  "open_ports": [22,5000,5001], "confidence": {"vendor":"high","type":"high","model":"medium"},
  "sources": ["arp","oui","mdns","port"] }
```

**資產盤查總覽**：PC / Mac / NAS / 防火牆 / 路由器 / AP / 印表機 / 掃描器 / 手機 / IoT / 不明——各類數量統計 + 明細表 + CSV/JSON 匯出。

**指定 IP 工具**：對單一 IP 做 ping、埠掃描、重新指紋。

**限制（會誠實標示）**：品牌 OUI 可靠；類型啟發式大致準；**精確型號只有 SNMP/mDNS/UPnP 有廣告的設備抓得到**，其餘顯示「品牌 + 類型」。

**API**：
```
GET  /api/lan/interfaces          → 本機網卡與網段清單
POST /api/lan/scan {subnet,deep}  → 啟動掃描
GET  /api/lan/scan/stream         → SSE 進度 + 逐台結果
GET  /api/lan/hosts               → 最近結果
POST /api/lan/ping {ip}           → ping 單一 IP
POST /api/lan/portscan {ip}       → 埠掃描
GET  /api/lan/export?fmt=csv|json → 匯出資產表
```

---

### 3.2 流量異常監測（`traffic_monitor.py`）🆕

**目標**：短時間監聽網卡流量，抓出「誰狂發封包、廣播/ARP 風暴、封包迴圈、IP 衝突、有人亂打 IP」。**不解碼封包內容**，只做統計。

**技術**：`scapy` 於混雜模式抓 L2/L3 標頭（需提權；Windows 需 Npcap）。若無提權/Npcap → 降級用作業系統統計（`netstat`/介面計數器）給粗略資訊並提示。

**偵測項目**：
| 現象 | 判斷邏輯 |
|---|---|
| **Top Talkers** | 依來源 MAC/IP 統計封包數與位元組，排行榜 |
| **廣播/群播風暴** | 廣播封包速率 > 門檻（可調），或占比異常高 |
| **ARP 風暴 / 掃描** | 單一來源短時間大量 ARP request |
| **封包迴圈 Loop** | 重複封包速率飆高 + 廣播暴增（STP 未生效徵兆） |
| **IP 衝突** | 同一 IP 出現在多個 MAC；或大量 gratuitous ARP |
| **Rogue IP / 亂打 IP** | 來源 IP 不在本網段/DHCP 範圍、或非法 IP |
| **可疑 flooding** | 單一來源封包量遠超其他（你要「抓爆量的人」） |

**輸出**：即時排行榜 + 異常事件清單（含來源 MAC/IP、速率、判定），時間軸圖。

**API**：
```
POST /api/traffic/start {iface,duration}
GET  /api/traffic/stream          → SSE 即時統計
POST /api/traffic/stop
GET  /api/traffic/result          → 異常摘要 + top talkers
```

---

### 3.3 網路診斷工具面板（`net_diag.py`）🆕

現場工程師常用指令，包成按鈕（跨平台，內部處理 mac/Windows 指令差異）：
- **Ping**（指定目標 + 連續）
- **Traceroute / tracert**
- **DNS 查詢**（nslookup/dig）
- **ARP 表 / 路由表**檢視
- **子網計算機**（輸入 IP/CIDR → 網段、可用範圍、廣播、主機數；現場心算替代）
- **NIC 連線速率讀取**（判斷實體協商速率 100M/1G/2.5G/10G）

**網路速度等級判斷**：綜合 ①NIC 協商速率 ②iperf3 內網實測 ③對外 speedtest → 給「等級判定」（例：實體 1G / 內網實測 940M / 對外 300M → 瓶頸在對外線路）。

---

### 3.4 AI 診斷按鈕組（擴充 `ai_advisor.py` + `recommendations.py`）🆕

**設計**：每個診斷情境一顆按鈕，點下去→蒐集對應數據→**離線規則引擎**判讀（預設）→有網路可選「進階雲端 AI」深入分析。輸出統一為「問題→原因→建議動作」卡片 + 嚴重度。

| 按鈕 | 蒐集的數據 | 判斷邏輯（離線規則） |
|---|---|---|
| **OSI 由下而上排查** | 網卡 link、IP、閘道 ping、DNS | 逐層檢查：實體→IP→閘道→DNS，指出斷在哪一層 |
| **Loop / 廣播風暴** | 流量監測結果 | 廣播率/重複封包超標 → 疑似 loop，建議查 STP |
| **VLAN 問題** | 網段、閘道、可見廣播網段 | 抓不到 DHCP / 跨網段異常 → 疑似 VLAN 標籤或 Native VLAN 不一致 |
| **DHCP 耗盡 / IP 衝突** | ARP、DHCP 探測、IP 衝突偵測 | 拿不到 IP / 同 IP 多 MAC → 定位問題 |
| **DNS 失敗 vs 真斷線** | ping IP vs ping 域名 vs DNS 查詢 | 通 IP 不通域名 → DNS 問題；都不通 → 線路問題 |
| **無線現場判讀** | Wi-Fi 掃描/熱圖/穩定度 | 覆蓋死角、干擾、通道規劃、roaming（沿用現有引擎） |
| **NAT / 路由 / 防火牆判讀** | 路由表、閘道、對外連通 | 判斷 NAT 是否正常、預設路由、對外阻擋 |

**一鍵全項健檢**：依序跑 Wi-Fi + LAN 盤查 + 流量 + 各層診斷 → 綜合健康分數 + 完整報告。

**AI 兩者並存實作**：
- 預設離線規則引擎（無依賴、離線可跑）。
- 設定頁可填 Claude API key（或登入）→ 啟用「進階 AI 分析」，把離線結果 + 原始數據送雲端做更深判讀。
- 無網路 / 無 key → 自動退回離線，UI 明確標示當前引擎。

---

### 3.5 雙語 i18n（中 / 英）🆕

- 前端字串抽到 `frontend/i18n/zh-TW.json` 與 `en.json`，以 `t("key")` 取用。
- 語言切換器（右上），記憶偏好。
- 報告 PDF 亦雙語（依當前語言產出）。
- 預設語言 ❓（繁中 / 英文 / 依系統）。

---

### 3.6 商用整合報告（擴充 `reporter.py`）🆕

- **一份完整檢測報告**整合：封面 → 摘要健康分數 → Wi-Fi（掃描/通道/熱圖/穩定度）→ LAN 資產盤查 → 流量異常 → AI 診斷建議 → 附錄數據。
- **白標** ❓：封面/頁首可放廠商 logo、公司名、聯絡資訊、案件編號、客戶名稱。
- 匯出 PDF（中/英），可存檔交付客戶。

---

## 4. 權限與相依（重要）

| 功能 | mac | Windows |
|---|---|---|
| Wi-Fi 掃描 | 需定位權限 | netsh（一般權限） |
| LAN ARP 掃描 / 流量抓取 | **需 root**（raw socket） | **需系統管理員 + 安裝 Npcap** |
| ICMP sweep / 埠掃描 | 一般權限可（備援） | 一般權限可 |

**設計對策**：
- App 啟動時偵測權限與 Npcap，缺少時**引導安裝/提權**（不是直接失敗）。
- 未提權時，LAN/流量功能**降級**執行（用 ICMP/OS 統計）並明確提示「提權可獲得完整結果」。
- Windows 安裝檔可選擇**一併打包 Npcap 安裝**。

---

## 5. 打包與發布

| 平台 | 產物 | 工具 |
|---|---|---|
| macOS | `.dmg`（拖曳安裝）／可選 `.pkg` | 現有 `build_dmg.sh`（已驗證可用） |
| Windows | `NetInspect-Setup.exe` | 現有 `build_windows.bat`（PyInstaller，**需在 Windows 機器上驗證**） |

- 兩者上傳 **GitHub Releases**，介紹頁（`docs/index.html`）加雙平台下載按鈕。
- 版本號統一管理（目前 2.1.0）。
- ⚠️ Windows 打包**必須在 Windows 環境**跑（無法在這台 mac 產出 .exe）；我可以把腳本與流程準備到位、寫好 GitHub Actions 自動在雲端 Windows 打包。❓ 是否要用 GitHub Actions 自動出雙平台安裝檔？

---

## 6. UI 藍圖（側邊分頁）

```
┌─ 現場健檢（一鍵全項）★首頁
├─ Wi-Fi
│   ├ 掃描 / 通道分析
│   ├ 頻譜分析儀（2D/3D）
│   ├ 覆蓋熱圖
│   ├ 即時監控
│   └ 穩定度
├─ 有線 LAN 🆕
│   ├ 網段掃描 / 資產盤查
│   ├ 設備明細（品牌/類型/型號）
│   └ 指定 IP 工具（ping/埠掃描）
├─ 流量監測 🆕（top talkers / 異常）
├─ 效能
│   ├ 網路效能（ping/DNS/HTTP）
│   ├ iperf3 吞吐量（含內網對 NAS）
│   └ 對外測速
├─ 診斷工具 🆕（ping/tracert/dns/arp/route/子網計算）
├─ AI 診斷 🆕（各情境按鈕 + 一鍵）
└─ 報告（整合 / 白標 / 匯出）
```

---

## 7. 分階段路線圖

| 階段 | 內容 | 產出 |
|---|---|---|
| **P0** | 跨平台架構整理、i18n 框架、（可選）改名 | 統一 code base、可切語言 |
| **P1** | LAN 盤查模組（掃描/指紋/資產表/指定 IP/內網速度） | 可用的地端盤查 + 匯出 |
| **P2** | 流量異常監測（輕量） | top talkers + 異常事件 |
| **P3** | 診斷工具面板 + AI 診斷按鈕組 + 一鍵全項 | 完整診斷體驗 |
| **P4** | 整合檢測報告 + 白標 | 可交付客戶的 PDF |
| **P5** | 雙平台打包（+GitHub Actions）+ 下載頁 + （選配）授權 | 兩個可下載安裝檔 |

> 每階段完成即 push GitHub 並讓你驗收，再進下一階段。

---

## 8. 待你確認的清單

1. **產品名稱**：用 NetInspect Pro？還是自訂？
2. **白標**：報告要放廠商 logo/公司抬頭嗎？要的話收集哪些欄位（logo、公司名、案件編號、客戶名…）？
3. **授權機制**：這版要不要 licence key 啟用？還是先免授權、後期再加？
4. **預設語言**：繁中 / 英文 / 跟隨系統？
5. **Windows 打包**：要不要用 GitHub Actions 自動在雲端 Windows 出 .exe？
6. **雲端 AI**：進階分析用 Claude API（你提供 key）還是走你現有的 Codex 登入？
7. **範圍確認**：SSH/Telnet(PuTTY)、TFTP、完整拓樸(The Dude) 我建議**排除**——同意嗎？
8. 規格本身有沒有要增刪的功能？

確認後我從 **P0 + P1** 開始動工。
