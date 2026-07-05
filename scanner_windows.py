"""
NetInspect Pro — Windows 掃描後端 v2
使用 netsh wlan，支援中文 / 英文 Windows（欄位名稱語系無關解析）
"""
import re
import subprocess
import time
import locale

# ── Radio / security maps ──────────────────────────────────────────────────
_RADIO_GEN = {
    "802.11b": 1, "802.11a": 1, "802.11g": 3,
    "802.11n": 4, "802.11ac": 5, "802.11ax": 6, "802.11be": 7,
}
_RADIO_MODE = {
    "802.11b": "b", "802.11a": "a", "802.11g": "b/g",
    "802.11n": "b/g/n", "802.11ac": "a/n/ac",
    "802.11ax": "a/n/ac/ax", "802.11be": "a/n/ac/ax/be",
}
_SEC_KEYWORDS = [
    ("wpa3", "WPA3"), ("wpa2", "WPA2"), ("wpa", "WPA"),
    ("wep", "WEP"),   ("open", "Open"), ("none", "Open"),
    ("無驗證", "Open"), ("無加密", "Open"),
]


# ── netsh runner ───────────────────────────────────────────────────────────
def _netsh(*args) -> str:
    """Run netsh and decode output using system codepage with fallbacks."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["netsh", "wlan", *args],
            capture_output=True,
            timeout=20,
            creationflags=flags,
        )
        raw = r.stdout
        # Try encodings: system locale → UTF-8 → common CJK pages → latin-1
        for enc in [locale.getpreferredencoding(False),
                    "utf-8", "cp950", "gbk", "cp936", "cp1252", "latin-1"]:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("latin-1")
    except Exception:
        return ""


# ── Language-agnostic field parsers ────────────────────────────────────────
def _kv_pairs(block: str) -> dict:
    """Extract all key:value pairs from a netsh block."""
    pairs = {}
    for line in block.split("\n"):
        m = re.match(r"\s*(.+?)\s*:\s*(.+)", line)
        if m:
            pairs[m.group(1).strip()] = m.group(2).strip()
    return pairs


def _find_signal(pairs: dict) -> int:
    """Find signal% by value pattern — language-independent."""
    for v in pairs.values():
        m = re.match(r"(\d+)\s*%", v.strip())
        if m:
            return int(m.group(1))
    return 0


def _find_radio(pairs: dict) -> str:
    """Find radio type — always '802.11x', language-independent."""
    for v in pairs.values():
        if re.match(r"802\.11", v.strip(), re.IGNORECASE):
            return v.strip()
    return ""


def _find_channel(pairs: dict) -> int:
    """Find channel number — small integer 1-165."""
    for v in pairs.values():
        s = v.strip()
        if re.match(r"^\d+$", s):
            ch = int(s)
            if 1 <= ch <= 165:
                return ch
    return 0


def _find_auth(pairs: dict) -> str:
    """Find authentication/security by keyword in value."""
    for v in pairs.values():
        vl = v.lower()
        for kw, label in _SEC_KEYWORDS:
            if kw in vl:
                return label
    return "Unknown"


# ── helpers ────────────────────────────────────────────────────────────────
def _pct_to_rssi(pct: int) -> int:
    return int(pct / 2 - 100)

def _band(ch: int) -> str:
    if ch <= 14:  return "2.4GHz"
    if ch <= 165: return "5GHz"
    return "6GHz"

def _width_guess(radio: str, band: str) -> str:
    rl = radio.lower()
    if "be" in rl:                      return "160"
    if "ax" in rl:                      return "80"
    if "ac" in rl and band == "5GHz":   return "80"
    if "n" in rl:                       return "40"
    return "20"


# ── Public API ─────────────────────────────────────────────────────────────
def scan_networks() -> list[dict]:
    """Return sorted list of Wi-Fi networks via netsh (language-agnostic)."""
    from scanner import (
        _assign_color, _update_seen, lookup_vendor,
        _quality, composite_quality_score, _channel_block,
        _seen_ago, _MAX_RATE,
    )

    output = _netsh("show", "networks", "mode=Bssid")
    if not output.strip():
        return []

    networks: list[dict] = []

    # SSID/BSSID keywords are always ASCII — split on them
    for ssid_block in re.split(r"SSID \d+ :", output)[1:]:
        ssid_lines = ssid_block.split("\n")
        ssid = ssid_lines[0].strip() or "<Hidden>"

        # Auth is in the SSID-level block (before first BSSID)
        pre_bssid = re.split(r"BSSID \d+ :", ssid_block)[0]
        auth = _find_auth(_kv_pairs(pre_bssid))

        for bblock in re.split(r"BSSID \d+ :", ssid_block)[1:]:
            blines = bblock.split("\n")
            bssid = blines[0].strip()
            if not bssid or not re.match(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", bssid, re.I):
                continue

            pairs    = _kv_pairs(bblock)
            sig_pct  = _find_signal(pairs)
            radio    = _find_radio(pairs)
            channel  = _find_channel(pairs)

            if sig_pct == 0 or channel == 0:
                continue

            rssi  = _pct_to_rssi(sig_pct)
            noise = -95
            snr   = rssi - noise
            band  = _band(channel)
            width = _width_guess(radio, band)
            gen   = _RADIO_GEN.get(radio.lower(), 4)
            mode  = _RADIO_MODE.get(radio.lower(), "b/g/n")

            color_key = bssid if not bssid.startswith("<") \
                        else f"__h_{channel}_{band}_{width}"
            color  = _assign_color(color_key)
            seen   = _update_seen(bssid or ssid)
            vendor = lookup_vendor(bssid)

            qkey, qlabel, qcolor = _quality(rssi, snr)
            qscore   = composite_quality_score(rssi, snr, noise)
            ch_block = _channel_block(channel, width, band)
            max_rate = _MAX_RATE.get((gen, int(width)))

            networks.append({
                "ssid": ssid, "bssid": bssid, "vendor": vendor,
                "rssi": rssi, "noise": noise, "snr": snr,
                "signal_pct": sig_pct, "band": band, "channel": channel,
                "width": width, "ch_block": ch_block, "mode": mode,
                "gen": gen, "max_rate": max_rate,
                "security": auth, "is_open": auth == "Open",
                "standard": radio, "quality": qkey,
                "quality_label": qlabel, "quality_color": qcolor,
                "quality_score": qscore,
                "station_count": None, "channel_util_pct": None,
                "net_color": color,
                "first_seen": seen["first_seen"], "last_seen": seen["last_seen"],
                "seen_ago": _seen_ago(seen["last_seen"]),
                "is_new": (time.time() - seen["first_seen"]) < 120,
            })

    networks.sort(key=lambda x: x["rssi"], reverse=True)
    return networks


def current_connection() -> dict:
    """Return current Wi-Fi connection (language-agnostic)."""
    from scanner import _quality, lookup_vendor

    output = _netsh("show", "interfaces")
    if not output.strip():
        return {"connected": False}

    # Check state keyword — common across locales: "connected" / "已連線"
    state_connected = bool(re.search(
        r":\s*(connected|已連線|已連接)", output, re.IGNORECASE))
    if not state_connected:
        return {"connected": False}

    pairs = _kv_pairs(output)

    # SSID / BSSID — find by value format
    bssid = ""
    ssid  = ""
    for v in pairs.values():
        if re.match(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", v, re.I) and not bssid:
            bssid = v.strip()
    # SSID: value that's a plain string (not a MAC, not a number, not a known keyword)
    for k, v in pairs.items():
        if v == bssid:
            continue
        if re.match(r"^\d+$", v) or re.match(r"^\d+%$", v):
            continue
        if "802.11" in v or "wpa" in v.lower() or "wep" in v.lower():
            continue
        # Heuristic: SSID key label contains "SSID" in any language
        if "ssid" in k.lower() and "bssid" not in k.lower():
            ssid = v.strip()
            break

    sig_pct = _find_signal(pairs)
    radio   = _find_radio(pairs)
    channel = _find_channel(pairs)
    auth    = _find_auth(pairs)

    # tx rate: find float value (e.g. "300")
    tx_rate = 0.0
    for v in pairs.values():
        m = re.match(r"^([\d.]+)$", v.strip())
        if m:
            f = float(m.group(1))
            if 1 < f < 10000:
                tx_rate = f
                break

    rssi  = _pct_to_rssi(sig_pct)
    noise = -95
    snr   = rssi - noise
    band  = _band(channel) if channel else "Unknown"
    gen   = _RADIO_GEN.get(radio.lower(), 4)

    qkey, qlabel, qcolor = _quality(rssi, snr)

    return {
        "connected": True, "ssid": ssid, "bssid": bssid,
        "vendor": lookup_vendor(bssid),
        "rssi": rssi, "noise": noise, "snr": snr,
        "signal_pct": sig_pct, "band": band, "channel": channel,
        "width": _width_guess(radio, band), "tx_rate": tx_rate,
        "standard": radio, "gen": gen,
        "quality": qkey, "quality_label": qlabel, "quality_color": qcolor,
    }


def get_supported_bands() -> list[str]:
    nets  = scan_networks()
    bands = {n["band"] for n in nets}
    return sorted(bands) if bands else ["2.4GHz", "5GHz"]
