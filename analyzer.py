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
    """Return 0.0–1.0 overlap factor between two channels."""
    span_a = OVERLAP_SPAN.get(w_a, 2) * 5  # MHz
    span_b = OVERLAP_SPAN.get(w_b, 2) * 5
    freq_a = _channel_center_mhz(ch_a, band)
    freq_b = _channel_center_mhz(ch_b, band)
    dist   = abs(freq_a - freq_b)
    max_no_overlap = (span_a + span_b) / 2
    if dist >= max_no_overlap:
        return 0.0
    return max(0.0, 1.0 - dist / max_no_overlap)


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

    # Score each occupied channel
    channel_scores: dict[int, dict] = {}
    for chan, occupants in chan_map.items():
        # Base score: number of networks × average signal strength weight
        base = len(occupants)
        # Add overlap penalty
        overlap_penalty = 0.0
        for other_chan, other_nets in chan_map.items():
            if other_chan == chan:
                continue
            for n in other_nets:
                ov = _overlap_score(chan, "20", other_chan,
                                    n.get("width", "20"), band)
                overlap_penalty += ov * 0.5

        score = min(100.0, base * 20 + overlap_penalty * 10)
        channel_scores[chan] = {
            "channel": chan,
            "occupants": base,
            "overlap_penalty": round(overlap_penalty, 2),
            "congestion_score": round(score, 1),
            "networks": [n["ssid"] for n in occupants],
            "rssi_values": [n["rssi"] for n in occupants],
        }

    # Find best (least congested) channels for each band
    best = _best_channels(band, channel_scores)

    return {
        "networks": nets,
        "channels": channel_scores,
        "best_channels": best,
        "occupied": sorted(chan_map.keys()),
    }


def _best_channels(band: str, scores: dict) -> list[dict]:
    """Return top-3 recommended channels with reasoning."""
    occupied_scores = scores  # keys = occupied channels

    if band == "2.4GHz":
        candidates = [1, 6, 11]
        result = []
        for c in candidates:
            info = occupied_scores.get(c, {})
            congestion = info.get("congestion_score", 0)
            occ        = info.get("occupants", 0)
            result.append({
                "channel": c, "congestion": congestion, "occupants": occ,
                "is_non_overlap": True,
                "reason": f"非重疊頻道 ch{c}，{occ} 個鄰居" if occ else f"非重疊頻道 ch{c}，目前無鄰居"
            })
        result.sort(key=lambda x: x["congestion"])
        return result

    if band == "5GHz":
        # Prefer UNII-1 (no DFS), then UNII-3, then DFS
        candidates = sorted(UNII1 | UNII3) + sorted(UNII2A) + sorted(UNII2C)
        result = []
        for c in candidates:
            info = occupied_scores.get(c, {})
            congestion = info.get("congestion_score", 0)
            occ        = info.get("occupants", 0)
            is_dfs     = c in DFS_5G
            result.append({
                "channel": c, "congestion": congestion, "occupants": occ,
                "is_dfs": is_dfs,
                "reason": f"{'DFS 頻道 ' if is_dfs else ''}ch{c}，{occ} 個鄰居"
            })
        result.sort(key=lambda x: (x["congestion"], x["is_dfs"]))
        return result[:5]

    if band == "6GHz":
        # 6 GHz generally less congested; prefer lower channels
        preferred = [1, 5, 9, 13, 17, 21, 37, 53, 69, 85]
        result = []
        for c in preferred:
            info = occupied_scores.get(c, {})
            congestion = info.get("congestion_score", 0)
            occ        = info.get("occupants", 0)
            result.append({
                "channel": c, "congestion": congestion, "occupants": occ,
                "reason": f"6GHz ch{c}（Wi-Fi 6E/7），{occ} 個鄰居"
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
    rssi_vals = [n["rssi"] for n in networks]
    snr_vals  = [n["snr"]  for n in networks]
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
        "has_wifi7_candidate": any("Wi-Fi 7" in n.get("standard", "") for n in networks),
    }
