"""
統一優化建議引擎 — 讓「每一個功能」跑完測試後都能取得對應的 Wi-Fi 優化建議方案。

設計原則：
  · 盡量重用 recommender.py 既有的 _check_* 規則（訊號 / SNR / 頻道 / 安全 / 效能…），
    各功能只挑與自己相關的檢查組合成「聚焦版」建議。
  · 輸出格式與 recommender.full_assessment 完全一致（score / grade / issues / counts），
    前端可用同一個 renderer 呈現。
  · 任何情況都至少回一條建議（沒有問題就回「✓ 通過」），確保「跑完一定有建議」。
"""
import statistics

import recommender
import analyzer

_ORDER = {"critical": 0, "warning": 1, "info": 2, "good": 3}
_DED   = {"critical": 20, "warning": 8, "info": 2, "good": 0}


def _pack(issues: list, ok_msg: str = "目前各項指標正常，無需調整。") -> dict:
    """把 issue 清單收斂成標準輸出（含分數 / 評級 / 計數）。"""
    if not any(i["severity"] in ("critical", "warning", "info") for i in issues):
        issues.append({
            "severity": "good", "category": "整體", "icon": "✅",
            "title": "未發現需要優化的項目", "problem": "",
            "recommendation": "✓ " + ok_msg, "value": "",
        })
    issues.sort(key=lambda x: _ORDER.get(x["severity"], 9))
    score = 100
    for i in issues:
        score -= _DED.get(i["severity"], 0)
    score = max(0, min(100, score))
    return {
        "score":  score,
        "grade":  recommender._grade(score),
        "issues": issues,
        "counts": {s: sum(1 for i in issues if i["severity"] == s)
                   for s in ("critical", "warning", "info", "good")},
    }


# ═══ 掃描（鄰居 AP 環境）═════════════════════════════════════════════════════
def assess_scan(networks: list, current: dict, analysis: dict = None) -> dict:
    if analysis is None:
        analysis = analyzer.analyze_channels(networks)
    issues = []
    if current.get("connected"):
        recommender._check_signal(current, issues)
        recommender._check_snr(current, issues)
        recommender._check_band(current, issues)
    recommender._check_channel_congestion(analysis, current, issues)
    recommender._check_co_channel(analysis, issues)
    recommender._check_24g_overlap(analysis, issues)
    recommender._check_security(networks, issues)
    recommender._check_neighbor_density(networks, issues)
    recommender._check_6g_availability(networks, current, issues)
    return _pack(issues, "鄰居環境單純、頻道規劃與安全性皆正常。")


# ═══ 網路效能（ping / jitter / loss / dns / http）════════════════════════════
def assess_netperf(perf: dict, current: dict) -> dict:
    issues = []
    recommender._check_ping(perf, issues)
    recommender._check_jitter(perf, issues)
    recommender._check_packet_loss(perf, issues)
    recommender._check_dns(perf, issues)
    _check_http(perf, issues)
    # 訊號層脈絡：效能差時，先確認不是訊號/SNR 造成
    if current.get("connected"):
        recommender._check_signal(current, issues)
        recommender._check_snr(current, issues)
    return _pack(issues, "延遲、抖動、丟包、DNS 皆在良好範圍。")


def _check_http(perf: dict, issues: list):
    ms = perf.get("http_ms")
    if ms is None:
        return
    if ms > 1000:
        issues.append({
            "severity": "warning", "category": "HTTP 響應", "icon": "🌐",
            "title": f"HTTP 首位元組緩慢（{ms} ms）",
            "problem": f"HTTP 取得首位元組耗時 {ms} ms，網頁/雲端服務開啟明顯卡頓。",
            "recommendation": "排查上游頻寬是否被佔用、AP 是否過載；啟用 QoS 優先網頁流量；必要時就近選擇 CDN 節點。",
            "value": ms,
        })
    elif ms <= 100:
        issues.append({
            "severity": "good", "category": "HTTP 響應", "icon": "🌐",
            "title": f"HTTP 響應快速（{ms} ms）✓", "problem": "",
            "recommendation": f"✓ HTTP {ms} ms，網頁與雲端服務反應流暢。", "value": ms,
        })


# ═══ 頻譜分析（聚焦單一頻段的頻道壅塞 / 最佳頻道）═════════════════════════════
def assess_spectrum(networks: list, band: str, current: dict) -> dict:
    analysis = analyzer.analyze_channels(networks)
    issues = []
    recommender._check_channel_congestion(analysis, current, issues)
    recommender._check_co_channel(analysis, issues)
    recommender._check_24g_overlap(analysis, issues)
    recommender._check_neighbor_density(networks, issues)

    bd   = analysis.get(band, {})
    best = bd.get("best_channels", [])
    band_nets = [n for n in networks if n.get("band") == band]
    if best:
        top = best[0]
        cur_ch = current.get("channel") if current.get("band") == band else None
        if cur_ch is not None and cur_ch != top["channel"]:
            issues.append({
                "severity": "info", "category": f"{band} 最佳頻道", "icon": "🎯",
                "title": f"{band} 建議改用頻道 {top['channel']}",
                "problem": f"您目前在 {band} 使用頻道 {cur_ch}，而頻道 {top['channel']} 壅塞最低。",
                "recommendation": f"將 AP 的 {band} 切換至頻道 {top['channel']}（本頻段最空），可降低同頻干擾、提升實際吞吐。",
                "value": top["channel"],
            })
        else:
            issues.append({
                "severity": "good", "category": f"{band} 最佳頻道", "icon": "🎯",
                "title": f"{band} 最佳頻道：ch {top['channel']}", "problem": "",
                "recommendation": f"✓ 在 {band} 規劃 AP 時，優先採用頻道 {top['channel']}（目前最空）。",
                "value": top["channel"],
            })
    if not band_nets:
        issues.append({
            "severity": "good", "category": f"{band} 頻譜", "icon": "📡",
            "title": f"{band} 目前無鄰居 AP", "problem": "",
            "recommendation": f"✓ {band} 頻段乾淨，是部署 AP 的理想頻段。", "value": 0,
        })
    return _pack(issues, f"{band} 頻段頻道配置健康。")


# ═══ 熱力圖（覆蓋死角）═══════════════════════════════════════════════════════
def assess_heatmap(measurements: list) -> dict:
    issues = []
    if not measurements:
        issues.append({
            "severity": "info", "category": "覆蓋量測", "icon": "🗺️",
            "title": "尚無量測點", "problem": "",
            "recommendation": "在各區域走點取樣（建議每 5–8 公尺一點），蒐集足夠樣本後即可分析覆蓋死角。",
            "value": 0,
        })
        return _pack(issues)
    recommender._check_coverage_gaps(measurements, issues)
    # 額外：覆蓋均勻度（標準差大代表訊號落差大、漫遊體驗差）
    rssis = [m["rssi"] for m in measurements if m.get("rssi") is not None]
    if len(rssis) >= 3:
        std = statistics.pstdev(rssis)
        if std > 12:
            issues.append({
                "severity": "warning", "category": "覆蓋均勻度", "icon": "📐",
                "title": f"覆蓋落差大（RSSI 標準差 {std:.0f} dB）",
                "problem": f"各量測點訊號從 {min(rssis)} 到 {max(rssis)} dBm，落差大，移動中漫遊體驗會不一致。",
                "recommendation": "重新調整 AP 佈點與功率，讓相鄰 AP 覆蓋重疊區維持 -67~-72 dBm，縮小強弱落差。",
                "value": round(std, 1),
            })
    return _pack(issues, "各量測點覆蓋均達標。")


# ═══ 即時監控（一段時間內的 RSSI / SNR 穩定度）═══════════════════════════════
def assess_monitor(samples: list, current: dict) -> dict:
    issues = []
    rssis = [s["rssi"] for s in samples if s.get("rssi") is not None]
    snrs  = [s["snr"]  for s in samples if s.get("snr")  is not None]

    if len(rssis) < 3:
        issues.append({
            "severity": "info", "category": "監控樣本", "icon": "⏳",
            "title": "監控時間過短", "problem": "",
            "recommendation": "建議持續監控至少 30–60 秒（在實際使用位置），才能反映真實的訊號穩定度。",
            "value": len(rssis),
        })
        return _pack(issues)

    rmin, rmax = min(rssis), max(rssis)
    rstd = statistics.pstdev(rssis)
    # RSSI 波動
    if rstd > 6:
        issues.append({
            "severity": "warning", "category": "訊號穩定度", "icon": "📶",
            "title": f"訊號波動偏大（標準差 {rstd:.1f} dB）",
            "problem": f"監控期間 RSSI 在 {rmin}～{rmax} dBm 間跳動，波動大會造成速率忽快忽慢。",
            "recommendation": "檢查路徑上是否有人員走動、門開關、微波爐等間歇干擾；固定位置使用時應穩定黏在同一台 AP。",
            "value": round(rstd, 1),
        })
    else:
        issues.append({
            "severity": "good", "category": "訊號穩定度", "icon": "📶",
            "title": f"訊號穩定（標準差 {rstd:.1f} dB）✓", "problem": "",
            "recommendation": f"✓ RSSI 波動小（{rmin}～{rmax} dBm），連線穩定。", "value": round(rstd, 1),
        })
    # 最低 RSSI 是否曾掉到弱訊
    if rmin < -75:
        issues.append({
            "severity": "warning", "category": "訊號低谷", "icon": "📉",
            "title": f"期間最低訊號達 {rmin} dBm",
            "problem": f"監控期間訊號一度掉到 {rmin} dBm（低於 -75 dBm 死角門檻），該瞬間可能掉線或卡頓。",
            "recommendation": "在此位置增設 AP 或調整現有 AP 位置/功率，避免訊號低谷。",
            "value": rmin,
        })
    # SNR 低谷
    if snrs:
        smin = min(snrs)
        if smin < 20:
            issues.append({
                "severity": "warning", "category": "SNR 低谷", "icon": "🔊",
                "title": f"期間 SNR 最低 {smin} dB",
                "problem": f"監控期間 SNR 一度低至 {smin} dB（低於 20 dB），背景雜訊偏高會推升封包錯誤率。",
                "recommendation": "排查 RF 干擾源；優先使用雜訊較低的 5GHz/6GHz；或避開壅塞頻道。",
                "value": smin,
            })
    if current.get("connected"):
        recommender._check_band(current, issues)
    return _pack(issues, "監控期間訊號與 SNR 皆穩定。")


# ═══ 統一分派 ════════════════════════════════════════════════════════════════
def assess(context: str, payload: dict, networks: list, current: dict) -> dict:
    """context: scan | netperf | spectrum | heatmap | monitor"""
    if context == "scan":
        return assess_scan(networks, current, payload.get("analysis"))
    if context == "netperf":
        return assess_netperf(payload.get("perf", payload), current)
    if context == "spectrum":
        return assess_spectrum(networks, payload.get("band", current.get("band", "2.4GHz")), current)
    if context == "heatmap":
        return assess_heatmap(payload.get("measurements", []))
    if context == "monitor":
        return assess_monitor(payload.get("samples", []), current)
    return _pack([{
        "severity": "info", "category": "未知", "icon": "❓",
        "title": f"未知的建議情境：{context}", "problem": "",
        "recommendation": "請改用支援的測試功能。", "value": "",
    }])
