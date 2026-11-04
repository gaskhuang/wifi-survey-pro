"""
Wi-Fi Site Recommender — 智慧分析引擎
輸入: 掃描結果 + 頻道分析 + 效能測試 + 目前連線
輸出: 優先排序的問題清單 + 具體優化建議 + 整體健康分數
"""
from typing import Optional

# ─── 共用 RSSI 門檻（dBm）：訊號檢查、熱圖覆蓋、報告文字一律引用這裡 ──────────────
DATA_RSSI      = -65   # 數據業務最佳門檻（≥ 為優）
VOICE_RSSI     = -67   # 語音 / 視訊 / 漫遊建議門檻
DEAD_ZONE_RSSI = -70   # 死角判定門檻（< 為死角）
ROAM_OVERLAP   = f"{VOICE_RSSI} ~ {DEAD_ZONE_RSSI} dBm"   # 相鄰 AP 覆蓋重疊區建議值


# ─── 分析主函式 ────────────────────────────────────────────────────────────────
def full_assessment(
    networks: list[dict],
    analysis: dict,
    current: dict,
    netperf: dict,
    measurements: list[dict] = None,
) -> dict:
    issues = []

    # ── 1. 目前連線品質 ──────────────────────────────────────────────────────
    if current.get("connected"):
        _check_signal(current, issues)
        _check_snr(current, issues)
        _check_txrate(current, issues)
        _check_wifi_gen(current, issues)
        _check_band(current, issues)

    # ── 2. 頻道配置 ──────────────────────────────────────────────────────────
    _check_channel_congestion(analysis, current, issues)
    _check_co_channel(analysis, issues)
    _check_24g_overlap(analysis, issues)

    # ── 3. 環境安全性 ────────────────────────────────────────────────────────
    _check_security(networks, issues)

    # ── 4. 網路效能 ──────────────────────────────────────────────────────────
    if netperf:
        _check_ping(netperf, issues)
        _check_jitter(netperf, issues)
        _check_packet_loss(netperf, issues)
        _check_dns(netperf, issues)

    # ── 5. AP 環境 ───────────────────────────────────────────────────────────
    _check_neighbor_density(networks, issues)
    _check_6g_availability(networks, current, issues)

    # ── 6. 熱圖量測 (若有) ──────────────────────────────────────────────────
    if measurements:
        _check_coverage_gaps(measurements, issues)

    # ── 計算整體健康分數 ──────────────────────────────────────────────────────
    score = _compute_score(issues, current, netperf)

    # ── 排序: critical → warning → info → good ──────────────────────────────
    order = {"critical": 0, "warning": 1, "info": 2, "good": 3}
    issues.sort(key=lambda x: order.get(x["severity"], 9))

    return {
        "score":    score,
        "grade":    _grade(score),
        "issues":   issues,
        "counts": {
            "critical": sum(1 for i in issues if i["severity"] == "critical"),
            "warning":  sum(1 for i in issues if i["severity"] == "warning"),
            "info":     sum(1 for i in issues if i["severity"] == "info"),
            "good":     sum(1 for i in issues if i["severity"] == "good"),
        }
    }


# ─── 個別檢查函式 ──────────────────────────────────────────────────────────────

def _num(v):
    """數值欄位：None / 非數字回傳 None（呼叫端據此略過檢查）。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _check_signal(cur: dict, issues: list):
    rssi = _num(cur.get("rssi"))
    if rssi is None:
        return   # 無 RSSI 資料就不評
    if rssi < DEAD_ZONE_RSSI:
        issues.append({
            "severity": "critical",
            "category": "訊號強度",
            "icon": "📶",
            "title": f"訊號過弱（{rssi} dBm）",
            "problem": f"目前 RSSI {rssi} dBm，已低於企業死角門檻 {DEAD_ZONE_RSSI} dBm。視訊會議、VoIP 容易斷線。",
            "recommendation": f"在距離 AP 更近的位置使用，或在此區域新增一台 AP（建議相鄰 AP 覆蓋重疊區保持 {ROAM_OVERLAP}）。",
            "value": rssi,
        })
    elif rssi < VOICE_RSSI:
        issues.append({
            "severity": "warning",
            "category": "訊號強度",
            "icon": "📶",
            "title": f"訊號偏弱（{rssi} dBm）",
            "problem": f"RSSI {rssi} dBm，低於語音/漫遊建議門檻 {VOICE_RSSI} dBm（接近 {DEAD_ZONE_RSSI} dBm 死角門檻），VoIP 通話品質可能不穩。",
            "recommendation": f"調整 AP 位置或功率；若企業環境建議「降功率多佈點」策略，覆蓋重疊區設在 {ROAM_OVERLAP}。",
            "value": rssi,
        })
    elif rssi < DATA_RSSI:
        issues.append({
            "severity": "info",
            "category": "訊號強度",
            "icon": "📶",
            "title": f"訊號普通（{rssi} dBm）",
            "problem": f"RSSI {rssi} dBm，可滿足一般上網，但略低於數據業務最佳門檻 {DATA_RSSI} dBm。",
            "recommendation": f"若有視訊需求，建議優化至 ≥ {DATA_RSSI} dBm。可調整 AP 天線方向或稍微靠近 AP。",
            "value": rssi,
        })
    else:
        issues.append({
            "severity": "good",
            "category": "訊號強度",
            "icon": "📶",
            "title": f"訊號優異（{rssi} dBm）",
            "problem": "",
            "recommendation": f"✓ 訊號強度符合企業標準（≥ {DATA_RSSI} dBm）。",
            "value": rssi,
        })


def _check_snr(cur: dict, issues: list):
    snr = _num(cur.get("snr"))
    if snr is None:
        return   # 缺 SNR 不可當 0 dB（會誤報 critical）
    noise = cur.get("noise")
    if noise is None:
        noise = "—"
    if snr < 15:
        issues.append({
            "severity": "critical",
            "category": "訊噪比 SNR",
            "icon": "🔊",
            "title": f"SNR 嚴重不足（{snr} dB，噪音 {noise} dBm）",
            "problem": f"SNR {snr} dB 遠低於 20 dB 門檻，背景射頻雜訊（{noise} dBm）干擾嚴重，封包錯誤率會大幅上升。",
            "recommendation": "排查 RF 干擾源（微波爐、無線電話、Zigbee 設備）；5GHz/6GHz 雜訊通常更低，建議切換頻段；或縮短與 AP 的距離。",
            "value": snr,
        })
    elif snr < 20:
        issues.append({
            "severity": "warning",
            "category": "訊噪比 SNR",
            "icon": "🔊",
            "title": f"SNR 偏低（{snr} dB）",
            "problem": f"SNR {snr} dB，介於 15–20 dB，連線品質已開始受干擾影響。",
            "recommendation": "嘗試切換至 5GHz（雜訊較少）；檢查附近有無 2.4GHz 干擾設備；或調整頻道避開壅塞。",
            "value": snr,
        })
    elif snr < 25:
        issues.append({
            "severity": "info",
            "category": "訊噪比 SNR",
            "icon": "🔊",
            "title": f"SNR 尚可（{snr} dB）",
            "problem": f"SNR {snr} dB，略低於企業建議值 25 dB。",
            "recommendation": "目前可正常使用，若需高品質語音/視訊，建議優化至 ≥ 25 dB。",
            "value": snr,
        })
    else:
        issues.append({
            "severity": "good",
            "category": "訊噪比 SNR",
            "icon": "🔊",
            "title": f"SNR 優異（{snr} dB）",
            "problem": "",
            "recommendation": f"✓ SNR {snr} dB，超過企業標準 25 dB。",
            "value": snr,
        })


def _check_txrate(cur: dict, issues: list):
    tx = _num(cur.get("tx_rate"))
    rssi = _num(cur.get("rssi"))
    if tx and rssi is not None and tx < 54 and rssi >= DEAD_ZONE_RSSI:
        issues.append({
            "severity": "warning",
            "category": "連線速率",
            "icon": "⚡",
            "title": f"Tx Rate 偏低（{tx} Mbps）",
            "problem": f"雖然訊號 {rssi} dBm 尚可，但協商速率僅 {tx} Mbps，可能是同頻干擾或舊設備相容問題。",
            "recommendation": "關閉 AP 上的 802.11b 低速率支援（Legacy Rate Control）；檢查是否有舊設備強迫全網降速；考慮切換至 5GHz/6GHz。",
            "value": tx,
        })


def _check_wifi_gen(cur: dict, issues: list):
    std = str(cur.get("standard") or "")
    gen = 0
    if "802.11be" in std or "Wi-Fi 7" in std: gen = 7
    elif "802.11ax" in std or "Wi-Fi 6" in std: gen = 6
    elif "802.11ac" in std or "Wi-Fi 5" in std: gen = 5
    elif "802.11n"  in std or "Wi-Fi 4" in std: gen = 4

    if gen == 0:
        pass
    elif gen >= 6:
        issues.append({
            "severity": "good",
            "category": "Wi-Fi 規格",
            "icon": "📡",
            "title": f"Wi-Fi {gen} ({std.split('(')[-1].replace(')','').strip()}) ✓",
            "problem": "",
            "recommendation": f"✓ 使用 Wi-Fi {gen}，支援 OFDMA、MU-MIMO 等高效能特性。",
            "value": gen,
        })
    elif gen == 5:
        issues.append({
            "severity": "info",
            "category": "Wi-Fi 規格",
            "icon": "📡",
            "title": f"Wi-Fi 5 (802.11ac)，建議升級",
            "problem": "Wi-Fi 5 缺少 OFDMA 技術，高密度環境下效能不如 Wi-Fi 6。",
            "recommendation": "若 AP 和用戶端支援，升級至 Wi-Fi 6 (802.11ax) 可提升高密度場景效能 40%+。",
            "value": gen,
        })
    else:
        issues.append({
            "severity": "warning",
            "category": "Wi-Fi 規格",
            "icon": "📡",
            "title": f"Wi-Fi {gen} 規格過舊，建議升級",
            "problem": f"Wi-Fi {gen} 不支援現代高效能功能（OFDMA、1024-QAM、BSS Coloring），在多用戶場景下效能受限。",
            "recommendation": "規劃升級至 Wi-Fi 6 (802.11ax) 或 Wi-Fi 6E（若場地有 6GHz 需求）的企業 AP。",
            "value": gen,
        })


def _check_band(cur: dict, issues: list):
    band = cur.get("band", "")
    if band == "2.4GHz":
        issues.append({
            "severity": "info",
            "category": "使用頻段",
            "icon": "📻",
            "title": "目前使用 2.4GHz 頻段",
            "problem": "2.4GHz 全球只有 3 個非重疊頻道（1/6/11），干擾源多（藍牙、微波爐），最大速率受限。",
            "recommendation": "若 AP 支援，切換至 5GHz（更多可用頻道、更高速率）或 6GHz（Wi-Fi 6E/7 最乾淨）；或啟用 Band Steering 讓 AP 自動引導到最佳頻段。",
            "value": band,
        })


def _congested_channels(analysis: dict) -> dict:
    """各頻段的壅塞頻道 {band: [(ch, occupants, score), …]}，依嚴重度排序。"""
    out = {}
    for band in ("2.4GHz", "5GHz", "6GHz"):
        channels = (analysis.get(band) or {}).get("channels") or {}
        lst = []
        for ch_str, info in channels.items():
            occ = _num(info.get("occupants")) or 0
            score = _num(info.get("congestion_score")) or 0
            if occ >= 6 or score >= 70:
                try:
                    lst.append((int(ch_str), int(occ), float(score)))
                except (TypeError, ValueError):
                    continue
        if lst:
            out[band] = sorted(lst, key=lambda x: (-x[2], -x[1], x[0]))
    return out


def _own_band(current: dict):
    """使用者目前連線的頻段；未連線 / 未知回傳 None。"""
    if current.get("connected") is False:
        return None
    return current.get("band") or None


def _check_channel_congestion(analysis: dict, current: dict, issues: list):
    """每個頻段最多一條壅塞項目（避免同一現象每個頻道各扣一次分）。
    嚴重度：自己的頻道壅塞 → critical/warning；未連線（純勘測）→ warning；
    非自己所在頻段 / 非自己頻道 → info。"""
    cur_chan = current.get("channel")
    own_band = _own_band(current)
    congested = _congested_channels(analysis)

    for band in ("2.4GHz", "5GHz", "6GHz"):
        band_data = analysis.get(band) or {}
        channels  = band_data.get("channels") or {}
        best      = band_data.get("best_channels") or []
        best_ch   = best[0]["channel"] if best else "?"
        lst = congested.get(band)

        if lst:
            own_hit = next((x for x in lst if x[0] == cur_chan), None) \
                if own_band == band else None
            if own_hit:
                sev = "critical" if own_hit[1] >= 6 else "warning"
            elif own_band is None:
                sev = "warning"
            else:
                sev = "info"
            worst = own_hit or lst[0]
            shown = "、".join(f"ch{c}（{o} 個 AP，{sc:.0f}/100）" for c, o, sc in lst[:4])
            more = f" 等 {len(lst)} 個頻道" if len(lst) > 4 else ""
            if own_hit:
                title = f"{band} 頻道 {worst[0]} 嚴重壅塞（{worst[1]} 個 AP，您正在使用）"
                note = "您目前就連在其中的頻道，會直接影響速度與延遲。"
                rec = (f"將 AP 切換至 {band} 頻道 {best_ch}（壅塞分數最低）；"
                       "若 AP 支援 802.11k/v/r，可搭配自動頻道選擇（ACS）。")
            elif own_band is None or own_band == band:
                title = (f"{band} 頻道 {worst[0]} 嚴重壅塞（{worst[1]} 個 AP）" if len(lst) == 1
                         else f"{band} 有 {len(lst)} 個壅塞頻道（最嚴重 ch{worst[0]}）")
                note = ("目前連線頻道不在其中，對現有連線影響有限；規劃 AP 時請避開。"
                        if own_band == band else "規劃或調整 AP 時請避開這些頻道。")
                rec = (f"將此頻段的 AP 改用 {band} 頻道 {best_ch}（壅塞分數最低）；"
                       "若 AP 支援 802.11k/v/r，可搭配自動頻道選擇（ACS）。")
            else:
                title = f"{band} 鄰居頻道壅塞（{len(lst)} 個頻道，非目前連線頻段）"
                note = f"您目前連在 {own_band}，此頻段壅塞不影響現有連線。"
                rec = f"若需在 {band} 部署或調整 AP，請改用頻道 {best_ch}，避開上述壅塞頻道。"
            issues.append({
                "severity": sev,
                "category": "頻道壅塞",
                "icon": "📡",
                "title": title,
                "problem": f"{band} 壅塞頻道：{shown}{more}，同頻干擾嚴重。{note}",
                "recommendation": rec,
                "value": worst[1],
            })
            continue

        # 無嚴重壅塞：中度佔用時提示更好的頻道（每頻段一條）
        for ch_str, info in channels.items():
            occ = _num(info.get("occupants")) or 0
            score = _num(info.get("congestion_score")) or 0
            try:
                ch = int(ch_str)
            except (TypeError, ValueError):
                continue
            if (occ >= 3 or score >= 40) and best and best[0]["channel"] != ch \
                    and best[0]["congestion"] < score * 0.5:
                issues.append({
                    "severity": "info",
                    "category": "頻道配置",
                    "icon": "🔄",
                    "title": f"{band} 有更好的頻道可選",
                    "problem": f"頻道 {ch} 壅塞分數 {score:.0f}，而頻道 {best_ch} 分數更低。",
                    "recommendation": f"考慮將 {band} AP 切換至頻道 {best_ch} 以降低同頻干擾。",
                    "value": score,
                })
                break  # one suggestion per band is enough


def _check_co_channel(analysis: dict, issues: list):
    coi = _num((analysis.get("summary") or {}).get("co_channel_hotspots")) or 0
    coi = int(coi)
    if coi < 1:
        return
    # 已由「頻道壅塞」涵蓋的頻段不重複扣 warning
    congested = _congested_channels(analysis)
    covered = 0
    for band in congested:
        chs = (analysis.get(band) or {}).get("channels") or {}
        covered += sum(1 for i in chs.values() if (_num(i.get("occupants")) or 0) > 2)
    uncovered = max(0, coi - covered)
    if uncovered >= 3:
        issues.append({
            "severity": "warning",
            "category": "同頻干擾",
            "icon": "⚡",
            "title": f"存在 {coi} 個同頻干擾熱區",
            "problem": f"有 {coi} 個頻道同時被多個 AP 使用，形成同頻干擾（Co-Channel Interference），實際吞吐量可能比理論值低 30–50%。",
            "recommendation": "重新規劃 AP 頻道分配；2.4GHz 嚴格使用 1/6/11；5GHz 善用 UNII-1 非 DFS 頻道；高密度環境降頻寬至 20MHz。",
            "value": coi,
        })
    else:
        extra = "（主要熱區已列於「頻道壅塞」項目）" if covered else ""
        issues.append({
            "severity": "info",
            "category": "同頻干擾",
            "icon": "⚡",
            "title": f"同頻干擾熱區 {coi} 個{extra}" if covered else f"輕微同頻干擾（{coi} 個頻道）",
            "problem": f"有 {coi} 個頻道存在多個 AP 同頻使用。",
            "recommendation": "持續監控；若速率下降，可調整頻道或降低 AP 發射功率以縮小覆蓋重疊區。",
            "value": coi,
        })


def _check_24g_overlap(analysis: dict, issues: list):
    band24 = analysis.get("2.4GHz", {})
    occupied = band24.get("occupied", [])
    non_overlap = {1, 6, 11}
    bad = [ch for ch in occupied if ch not in non_overlap]
    if bad:
        issues.append({
            "severity": "warning",
            "category": "2.4GHz 頻道規劃",
            "icon": "📻",
            "title": f"偵測到 2.4GHz 非標準頻道（{bad}）",
            "problem": f"鄰近有 AP 使用頻道 {bad}，這些頻道與 1/6/11 重疊，會對所有相鄰頻道造成干擾。",
            "recommendation": "若您能控制這些 AP，請強制設定為 1、6 或 11 其中之一；若為鄰居 AP，選擇與它們重疊最少的頻道（1/6/11 中最空的一個）。",
            "value": bad,
        })


def _check_security(networks: list, issues: list):
    def sec(n):
        return str(n.get("security") or "")
    open_nets = [n for n in networks
                 if n.get("is_open") and not (n.get("ssid") or "").startswith("<")]
    if open_nets:
        names = ", ".join((n.get("ssid") or "(未命名)") for n in open_nets[:3])
        issues.append({
            "severity": "warning",
            "category": "安全性",
            "icon": "🔓",
            "title": f"偵測到 {len(open_nets)} 個開放網路",
            "problem": f"以下網路無加密：{names}。開放網路中的流量可被任何人監聽。",
            "recommendation": "將開放網路改為 WPA3 或至少 WPA2-PSK；訪客網路建議使用強制網頁登入（Captive Portal）而非完全開放。",
            "value": len(open_nets),
        })

    wpa3_nets = sum(1 for n in networks if "WPA3" in sec(n))
    if wpa3_nets == 0 and len(networks) > 0:
        issues.append({
            "severity": "info",
            "category": "安全性",
            "icon": "🔐",
            "title": "環境中無 WPA3 網路",
            "problem": "所有偵測到的網路均使用 WPA2 或更舊加密，缺乏 WPA3 的 SAE 驗證和個人化資料加密。",
            "recommendation": "若 AP 韌體支援，啟用 WPA3/WPA2 混合模式（Transition Mode）；新採購 AP 應選擇 WPA3 認證設備。",
            "value": 0,
        })
    elif wpa3_nets > 0:
        issues.append({
            "severity": "good",
            "category": "安全性",
            "icon": "🔐",
            "title": f"偵測到 {wpa3_nets} 個 WPA3 網路 ✓",
            "problem": "",
            "recommendation": "✓ 環境中有 WPA3 加密網路，安全性符合現代標準。",
            "value": wpa3_nets,
        })


def _check_ping(perf: dict, issues: list):
    ms = perf.get("ping_ms")
    if ms is None: return
    if ms > 80:
        issues.append({
            "severity": "critical",
            "category": "網路延遲",
            "icon": "⏱",
            "title": f"Ping 延遲嚴重（{ms} ms）",
            "problem": f"Ping 平均 {ms} ms，超過 80ms 門檻，VoIP/視訊會議通話品質差，遊戲幾乎無法使用。",
            "recommendation": "排查網路壅塞（AP 過載、ISP 線路問題）；啟用 QoS 優先保障即時流量；檢查有無大量下載佔用頻寬。",
            "value": ms,
        })
    elif ms > 30:
        issues.append({
            "severity": "warning",
            "category": "網路延遲",
            "icon": "⏱",
            "title": f"Ping 延遲偏高（{ms} ms）",
            "problem": f"Ping {ms} ms，超過 30ms，視訊通話可能有輕微延遲感。",
            "recommendation": "檢查 ISP 線路品質；優化 AP 負載；若使用 5GHz 可降低空口延遲。",
            "value": ms,
        })
    else:
        issues.append({
            "severity": "good",
            "category": "網路延遲",
            "icon": "⏱",
            "title": f"Ping 延遲優異（{ms} ms）✓",
            "problem": "",
            "recommendation": f"✓ Ping {ms} ms，低延遲網路，適合 VoIP 和即時應用。",
            "value": ms,
        })


def _check_jitter(perf: dict, issues: list):
    jitter = perf.get("ping_jitter_ms")
    if jitter is None: return
    if jitter > 15:
        issues.append({
            "severity": "warning",
            "category": "網路抖動",
            "icon": "〰️",
            "title": f"Ping Jitter 偏高（±{jitter} ms）",
            "problem": f"Jitter {jitter} ms 超過 15ms，延遲不穩定，語音封包到達時間不一致，通話品質差。",
            "recommendation": "啟用 AP 的 WMM（Wi-Fi Multi-Media）QoS；確認 AP 未過載；5GHz 環境 jitter 通常較低。",
            "value": jitter,
        })
    elif jitter > 5:
        issues.append({
            "severity": "info",
            "category": "網路抖動",
            "icon": "〰️",
            "title": f"Ping Jitter 尚可（±{jitter} ms）",
            "problem": f"Jitter {jitter} ms，一般上網無影響，但高品質語音通話建議低於 5ms。",
            "recommendation": "若需保障語音品質，考慮啟用 802.11r 快速漫遊和 WMM-Admission Control。",
            "value": jitter,
        })
    else:
        issues.append({
            "severity": "good",
            "category": "網路抖動",
            "icon": "〰️",
            "title": f"Jitter 優異（±{jitter} ms）✓",
            "problem": "",
            "recommendation": f"✓ Jitter {jitter} ms，延遲穩定，適合高品質語音和即時應用。",
            "value": jitter,
        })


def _check_packet_loss(perf: dict, issues: list):
    loss = perf.get("ping_loss_pct")
    if loss is None: return
    if loss > 5:
        issues.append({
            "severity": "critical",
            "category": "封包遺失",
            "icon": "📦",
            "title": f"封包遺失嚴重（{loss}%）",
            "problem": f"封包遺失率 {loss}%，遠超 2% 門檻，TCP 連線會頻繁重傳，實際吞吐量可能只有理論值的 1/5。",
            "recommendation": "立即檢查 AP 硬體狀態和韌體；排查 RF 干擾（掃描頻譜）；確認 AP 連線到 Switch 的有線品質。",
            "value": loss,
        })
    elif loss > 2:
        issues.append({
            "severity": "warning",
            "category": "封包遺失",
            "icon": "📦",
            "title": f"封包遺失偏高（{loss}%）",
            "problem": f"封包遺失 {loss}%，超過 2% 企業門檻，下載/串流會有卡頓。",
            "recommendation": "降低 AP 與用戶端距離；排查同頻干擾；考慮增加 AP 覆蓋點。",
            "value": loss,
        })
    elif loss == 0:
        issues.append({
            "severity": "good",
            "category": "封包遺失",
            "icon": "📦",
            "title": "封包遺失 0% ✓",
            "problem": "",
            "recommendation": "✓ 無封包遺失，網路連線穩定。",
            "value": 0,
        })


def _check_dns(perf: dict, issues: list):
    dns = perf.get("dns_ms")
    if dns is None: return
    if dns > 150:
        issues.append({
            "severity": "warning",
            "category": "DNS 解析",
            "icon": "🌐",
            "title": f"DNS 解析緩慢（{dns} ms）",
            "problem": f"DNS 解析耗時 {dns} ms，超過 150ms，每次開啟新網頁都會額外延遲。",
            "recommendation": "更換更快的 DNS 伺服器（如 1.1.1.1 Cloudflare 或 8.8.8.8 Google）；或在 AP/Router 啟用本地 DNS 快取。",
            "value": dns,
        })
    elif dns <= 20:
        issues.append({
            "severity": "good",
            "category": "DNS 解析",
            "icon": "🌐",
            "title": f"DNS 解析快速（{dns} ms）✓",
            "problem": "",
            "recommendation": f"✓ DNS {dns} ms，網頁開啟響應快。",
            "value": dns,
        })


def _check_neighbor_density(networks: list, issues: list):
    total = len(networks)
    bands = {}
    for n in networks:
        b = n.get("band")
        if b: bands[b] = bands.get(b, 0) + 1

    # 已有「頻道壅塞」項目時同一現象不再重複扣 warning
    has_congestion = any(i.get("category") == "頻道壅塞" for i in issues)
    if total >= 20:
        issues.append({
            "severity": "info" if has_congestion else "warning",
            "category": "AP 密度",
            "icon": "🏢",
            "title": f"高密度環境（{total} 個鄰居 AP）",
            "problem": f"偵測到 {total} 個鄰近 Wi-Fi 網路，為高密度環境，同頻干擾風險高。",
            "recommendation": "採用高密度部署策略：降低 AP 發射功率（Tx Power -3~-6 dBm）、縮小頻道寬度（40MHz→20MHz）、關閉 802.11b 低速率（11b disabled）、開啟 Band Steering 引導用戶到 5GHz。",
            "value": total,
        })
    elif total >= 10:
        issues.append({
            "severity": "info",
            "category": "AP 密度",
            "icon": "🏢",
            "title": f"中密度環境（{total} 個鄰居 AP）",
            "problem": f"偵測到 {total} 個鄰近網路，為中密度環境，需適當的頻道規劃。",
            "recommendation": "確保 2.4GHz 使用 1/6/11 非重疊頻道；5GHz 優先選用非 DFS 頻道（UNII-1/3）。",
            "value": total,
        })


def _check_6g_availability(networks: list, current: dict, issues: list):
    has_6g = any(n.get("band") == "6GHz" for n in networks)
    cur_band = current.get("band", "")
    if has_6g and cur_band != "6GHz":
        issues.append({
            "severity": "info",
            "category": "6GHz 升級機會",
            "icon": "🚀",
            "title": "環境有 Wi-Fi 6E/7 可用，但未連接",
            "problem": "偵測到 6GHz 頻段網路（Wi-Fi 6E/7），但您目前使用的是較擁擠的頻段。",
            "recommendation": "6GHz 頻段干擾最少、速率最高，若您的裝置支援 Wi-Fi 6E，請優先連接 6GHz SSID 以獲得最佳體驗。",
            "value": "6GHz available",
        })
    elif has_6g and cur_band == "6GHz":
        issues.append({
            "severity": "good",
            "category": "6GHz 頻段",
            "icon": "🚀",
            "title": "連接在 6GHz（Wi-Fi 6E/7）最佳頻段 ✓",
            "problem": "",
            "recommendation": "✓ 正在使用 6GHz 頻段，享有最低干擾和最高速率。",
            "value": "6GHz",
        })


def _check_coverage_gaps(measurements: list, issues: list):
    if not measurements: return
    pts = [_num(m.get("rssi")) for m in measurements if isinstance(m, dict)]
    pts = [r for r in pts if r is not None]      # 缺 RSSI 的量測點不納入
    if not pts: return
    weak = [r for r in pts if r < DEAD_ZONE_RSSI]
    total = len(pts)
    if weak:
        pct = len(weak) / total * 100
        issues.append({
            "severity": "critical" if pct > 30 else "warning",
            "category": "覆蓋死角",
            "icon": "🗺️",
            "title": f"熱圖量測發現 {len(weak)} 個死角（{pct:.0f}% 區域訊號不足）",
            "problem": f"在 {total} 個量測點中，{len(weak)} 個（{pct:.0f}%）的 RSSI 低於 {DEAD_ZONE_RSSI} dBm 死角門檻。",
            "recommendation": f"在死角位置增設 AP；參考熱圖確認最佳 AP 位置；企業環境建議 AP 覆蓋重疊率達 20%（覆蓋邊緣 {ROAM_OVERLAP}）。",
            "value": len(weak),
        })
    else:
        avg_rssi = sum(pts) / total
        issues.append({
            "severity": "good",
            "category": "覆蓋品質",
            "icon": "🗺️",
            "title": f"覆蓋量測全數達標（均值 {avg_rssi:.0f} dBm）✓",
            "problem": "",
            "recommendation": f"✓ {total} 個量測點均 ≥ {DEAD_ZONE_RSSI} dBm，覆蓋品質良好。",
            "value": avg_rssi,
        })


# ─── 分數計算 ──────────────────────────────────────────────────────────────────
def _compute_score(issues: list, current: dict, netperf: dict) -> int:
    score = 100
    deductions = {"critical": 20, "warning": 8, "info": 2, "good": 0}
    for i in issues:
        score -= deductions.get(i["severity"], 0)

    # Bonus: connection info available
    if not current.get("connected"):
        score -= 15

    # Net perf bonus/penalty
    if netperf:
        ping = netperf.get("ping_ms")
        if ping and ping <= 10: score += 3
        if ping and ping > 80:  score -= 5

    return max(0, min(100, score))


def _grade(score: int) -> dict:
    if score >= 90: return {"label": "優異", "color": "#22c55e", "emoji": "🟢"}
    if score >= 75: return {"label": "良好", "color": "#84cc16", "emoji": "🟡"}
    if score >= 55: return {"label": "普通", "color": "#eab308", "emoji": "🟠"}
    if score >= 35: return {"label": "較差", "color": "#f97316", "emoji": "🔴"}
    return {"label": "嚴重問題", "color": "#ef4444", "emoji": "🚨"}
