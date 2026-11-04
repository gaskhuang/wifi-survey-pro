"""
Wi-Fi Channel Analyzer
2.4 / 5 / 6 GHz congestion, overlap detection, enterprise recommendations
Based on 2025-2026 best practices (Wi-Fi 6/6E/7 aware)
"""
from typing import TypedDict

# ── 2.4 GHz: only 3 non-overlapping channels globally (1/6/11) ──────────────
NON_OVERLAP_24 = {1, 6, 11}

# ── 5 GHz channel groups (UNII bands) ───────────────────────────────────────
UNII1     = {36, 40, 44, 48}
UNII2A    = {52, 56, 60, 64}          # DFS
UNII2C    = set(range(100, 145, 4))   # DFS
UNII3     = {149, 153, 157, 161, 165}
DFS_5G    = UNII2A | UNII2C

# ── 6 GHz channels (Wi-Fi 6E / Wi-Fi 7) ────────────────────────────────────
# 1-233, step 4 starting at 1
CHANNELS_6G = set(range(1, 234, 4))

# Channel width overlap spans (half-width in channel units, 5 MHz per unit)
OVERLAP_SPAN = {
    "20":   2,   # ±10 MHz  → ±2 channels @ 5 MHz
    "40":   4,
    "80":   8,
    "160": 16,
    "80+80": 8,
    "320": 32,
}


class ChannelRec(TypedDict):
    band: str
    channel: int
    score: float        # 0–100, higher = more congested
    occupants: int
    recommendation: str
    reason: str


def _channel_center_mhz(chan: int, band: str) -> int:
    if band == "2.4GHz":
        return 2407 + chan * 5
    if band == "5GHz":
        return 5000 + chan * 5
    if band == "6GHz":
        return 5950 + chan * 5
    return 0


def _overlap_score(ch_a: int, w_a: str, ch_b: int, w_b: str, band: str) -> float:
    """Return 0.0–1.0 overlap factor between two channels.
    OVERLAP_SPAN 為半寬（頻道單位 × 5 MHz）；兩訊號中心距離 ≥ 半寬相加即不重疊。"""
    span_a = OVERLAP_SPAN.get(str(w_a), 2) * 5  # MHz（半寬）
    span_b = OVERLAP_SPAN.get(str(w_b), 2) * 5
    freq_a = _channel_center_mhz(ch_a, band)
    freq_b = _channel_center_mhz(ch_b, band)
    dist   = abs(freq_a - freq_b)
    max_no_overlap = span_a + span_b
    if dist >= max_no_overlap:
        return 0.0
    return max(0.0, 1.0 - dist / max_no_overlap)


# 5 GHz 可綁定的頻段區段（20 MHz 主頻道起點, 子頻道數）
_SEG_5G = [(36, 8), (100, 12), (149, 8)]


def _width_mhz(w) -> int:
    try:
        return int(str(w).split("+")[0])
    except (TypeError, ValueError):
        return 20


def _occupied_subchannels(ch: int, width, band: str) -> list[int]:
    """寬頻道（40/80/160/320 MHz）實際佔用的 20 MHz 子頻道。
    例：5 GHz ch36 @80 MHz → [36, 40, 44, 48]。無法對齊時只回主頻道。"""
    n = max(1, _width_mhz(width) // 20)
    if band == "5GHz":
        for start, count in _SEG_5G:
            if start <= ch < start + count * 4 and (ch - start) % 4 == 0:
                i = (ch - start) // 4
                first = (i // n) * n
                if n > 1 and first + n > count:
                    return [ch]          # 此區段放不下這個頻寬
                return [start + 4 * (first + j) for j in range(n)]
        return [ch]
    if band == "6GHz":
        if ch < 1 or (ch - 1) % 4:
            return [ch]
        i = (ch - 1) // 4
        first = (i // n) * n
        return [1 + 4 * (first + j) for j in range(n)]
    return [ch]


def _interference(band: str, cand: int, nets: list[dict]) -> tuple[float, int]:
    """候選頻道（以 20 MHz 部署）受所有鄰居的干擾：回傳 (加權和, 重疊 AP 數)。
    2.4 GHz 用頻譜重疊比例；5/6 GHz 依頻寬區塊：主頻道相同 1.0、落在其寬頻道內 0.8。"""
    total, count = 0.0, 0
    for n in nets:
        ch = n.get("channel")
        if not isinstance(ch, int):
            try:
                ch = int(ch)
            except (TypeError, ValueError):
                continue
        w = n.get("width") or "20"
        if band == "2.4GHz":
            ov = 1.0 if ch == cand else _overlap_score(cand, "20", ch, str(w), band)
        elif ch == cand:
            ov = 1.0
        else:
            ov = 0.8 if cand in _occupied_subchannels(ch, w, band) else 0.0
        if ov > 0:
            total += ov
            count += 1
    return total, count


def analyze_channels(networks: list[dict]) -> dict:
    """
    Given a list of scanned networks, return:
    - per-band congestion maps
    - recommended channels
    - enterprise guidance
    """
    bands = {"2.4GHz": [], "5GHz": [], "6GHz": []}
    for n in networks:
        b = n.get("band")
        if b in bands:
            bands[b].append(n)

    result = {}
    for band, nets in bands.items():
        result[band] = _analyze_band(band, nets)

    result["recommendations"] = _build_recommendations(result)
    result["summary"] = _build_summary(networks)
    return result


def _analyze_band(band: str, nets: list[dict]) -> dict:
    if not nets:
        return {"networks": [], "channels": {}, "best_channels": []}

    # Build channel occupancy map
    chan_map: dict[int, list[dict]] = {}
    for n in nets:
        c = n.get("channel", 0)
        if c not in chan_map:
            chan_map[c] = []
        chan_map[c].append(n)

    # Score each occupied channel（與候選頻道同一套干擾計算，含寬頻道重疊）
    channel_scores: dict[int, dict] = {}
    for chan, occupants in chan_map.items():
        base = len(occupants)                       # 同主頻道 AP 數
        weighted, _ = _interference(band, chan, nets)
        overlap_penalty = max(0.0, weighted - base)  # 來自相鄰 / 寬頻道的重疊
        score = min(100.0, weighted * 20)
        channel_scores[chan] = {
            "channel": chan,
            "occupants": base,
            "overlap_penalty": round(overlap_penalty, 2),
            "congestion_score": round(score, 1),
            "networks": [n.get("ssid") for n in occupants],
            "rssi_values": [n.get("rssi") for n in occupants],
        }

    # Find best (least congested) channels for each band
    best = _best_channels(band, channel_scores, nets)

    return {
        "networks": nets,
        "channels": channel_scores,
        "best_channels": best,
        "occupied": sorted(chan_map.keys()),
    }


def _best_channels(band: str, scores: dict, nets: list[dict] | None = None) -> list[dict]:
    """Return recommended channels with reasoning.
    每個候選頻道都對全部鄰居計算重疊（含 40/80/160 MHz 寬頻道與 2.4 GHz 相鄰頻道），
    不再只看「主頻道是否有人」。"""
    nets = nets or []

    def cand(c):
        if nets:
            weighted, overlap_n = _interference(band, c, nets)
            congestion = round(min(100.0, weighted * 20), 1)
        else:   # 舊呼叫方式（只給 scores）
            info = scores.get(c, {})
            congestion = info.get("congestion_score", 0)
            overlap_n = info.get("occupants", 0)
        co = (scores.get(c) or {}).get("occupants", 0)   # 同主頻道 AP 數
        return congestion, co, overlap_n

    if band == "2.4GHz":
        result = []
        for c in (1, 6, 11):
            congestion, co, ov_n = cand(c)
            if co:
                reason = f"非重疊頻道 ch{c}，{co} 個鄰居"
                if ov_n > co:
                    reason += f"，另有 {ov_n - co} 個相鄰頻道重疊"
            elif ov_n:
                reason = f"非重疊頻道 ch{c}，無同頻鄰居，但有 {ov_n} 個相鄰頻道 AP 重疊"
            else:
                reason = f"非重疊頻道 ch{c}，目前無鄰居"
            result.append({
                "channel": c, "congestion": congestion, "occupants": max(co, ov_n),
                "is_non_overlap": True, "reason": reason,
            })
        result.sort(key=lambda x: x["congestion"])
        return result

    if band == "5GHz":
        # Prefer UNII-1 (no DFS), then UNII-3, then DFS
        candidates = sorted(UNII1 | UNII3) + sorted(UNII2A) + sorted(UNII2C)
        result = []
        for c in candidates:
            congestion, co, ov_n = cand(c)
            is_dfs = c in DFS_5G
            wide = f"（含 {ov_n - co} 個寬頻道重疊）" if ov_n > co else ""
            result.append({
                "channel": c, "congestion": congestion, "occupants": ov_n,
                "is_dfs": is_dfs,
                "reason": f"{'DFS 頻道 ' if is_dfs else ''}ch{c}，{ov_n} 個鄰居{wide}",
            })
        result.sort(key=lambda x: (x["congestion"], x["is_dfs"]))
        return result[:5]

    if band == "6GHz":
        # 6 GHz generally less congested; prefer lower channels
        preferred = [1, 5, 9, 13, 17, 21, 37, 53, 69, 85]
        result = []
        for c in preferred:
            congestion, co, ov_n = cand(c)
            wide = f"（含 {ov_n - co} 個寬頻道重疊）" if ov_n > co else ""
            result.append({
                "channel": c, "congestion": congestion, "occupants": ov_n,
                "reason": f"6GHz ch{c}（Wi-Fi 6E/7），{ov_n} 個鄰居{wide}",
            })
        result.sort(key=lambda x: x["congestion"])
        return result[:5]

    return []


def _build_recommendations(analysis: dict) -> list[dict]:
    recs = []

    # 2.4 GHz
    b24 = analysis.get("2.4GHz", {})
    best24 = b24.get("best_channels", [])
    if best24:
        top = best24[0]
        recs.append({
            "band": "2.4GHz",
            "action": "建議頻道",
            "value": f"ch {top['channel']}",
            "detail": f"壅塞分數 {top['congestion']:.0f}/100，{top['occupants']} 個鄰居使用此頻道",
            "level": "info" if top["congestion"] < 40 else "warning",
        })

    # 5 GHz
    b5 = analysis.get("5GHz", {})
    best5 = b5.get("best_channels", [])
    if best5:
        top5 = [b for b in best5 if not b.get("is_dfs", False)][:1]
        if top5:
            top = top5[0]
            recs.append({
                "band": "5GHz",
                "action": "建議頻道（非DFS）",
                "value": f"ch {top['channel']}",
                "detail": f"壅塞分數 {top['congestion']:.0f}/100，{top['occupants']} 個鄰居",
                "level": "info" if top["congestion"] < 40 else "warning",
            })

    # 6 GHz
    b6 = analysis.get("6GHz", {})
    best6 = b6.get("best_channels", [])
    if best6:
        top = best6[0]
        recs.append({
            "band": "6GHz",
            "action": "Wi-Fi 6E/7 建議頻道",
            "value": f"ch {top['channel']}",
            "detail": f"6GHz 頻段干擾少，壅塞分數 {top['congestion']:.0f}/100",
            "level": "success",
        })

    # High-density check
    total_nets = sum(
        len(analysis.get(b, {}).get("networks", []))
        for b in ("2.4GHz", "5GHz", "6GHz")
    )
    if total_nets >= 15:
        recs.append({
            "band": "ALL",
            "action": "高密度環境",
            "value": f"{total_nets} 個鄰居 AP",
            "detail": "建議：降頻寬（80→40→20MHz）、開 Band Steering、關 802.11b 低速率",
            "level": "warning",
        })

    return recs


def _build_summary(networks: list[dict]) -> dict:
    if not networks:
        return {}
    rssi_vals = [n["rssi"] for n in networks if isinstance(n.get("rssi"), (int, float))] or [0]
    snr_vals  = [n["snr"]  for n in networks if isinstance(n.get("snr"), (int, float))] or [0]
    bands = {}
    for n in networks:
        b = n.get("band", "Unknown")
        bands[b] = bands.get(b, 0) + 1

    # Check co-channel interference (same channel, overlapping RSSI)
    chan_groups: dict[tuple, list] = {}
    for n in networks:
        key = (n.get("band"), n.get("channel"))
        if key not in chan_groups:
            chan_groups[key] = []
        chan_groups[key].append(n)
    coi_count = sum(1 for v in chan_groups.values() if len(v) > 2)

    return {
        "total_networks":      len(networks),
        "bands":               bands,
        "rssi_min":            min(rssi_vals),
        "rssi_max":            max(rssi_vals),
        "snr_min":             min(snr_vals),
        "snr_max":             max(snr_vals),
        "co_channel_hotspots": coi_count,
        "has_6ghz":            any(n.get("band") == "6GHz" for n in networks),
        "has_wifi7_candidate": any("Wi-Fi 7" in str(n.get("standard") or "") for n in networks),
    }
