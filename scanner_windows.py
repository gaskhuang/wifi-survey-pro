"""
Wi-Fi Survey Pro — Windows 掃描後端
使用 netsh wlan 指令取得 WiFi 資訊（不需要額外套件）
"""
import re
import subprocess
import time

# ── Radio type → gen / mode ────────────────────────────────────────────────
_RADIO_GEN = {
    "802.11b": 1, "802.11a": 1, "802.11g": 3,
    "802.11n": 4, "802.11ac": 5, "802.11ax": 6, "802.11be": 7,
}
_RADIO_MODE = {
    "802.11b":  "b",          "802.11a":  "a",
    "802.11g":  "b/g",        "802.11n":  "b/g/n",
    "802.11ac": "a/n/ac",     "802.11ax": "a/n/ac/ax",
    "802.11be": "a/n/ac/ax/be",
}
_SEC_MAP = {
    "open": "Open", "wep": "WEP",
    "wpa-personal": "WPA", "wpa2-personal": "WPA2", "wpa3-personal": "WPA3",
    "wpa-enterprise": "WPA-Ent", "wpa2-enterprise": "WPA2-Ent",
    "wpa3-enterprise": "WPA3-Ent",
}


def _netsh(*args) -> str:
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        r = subprocess.run(
            ["netsh", "wlan", *args],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=20, creationflags=flags,
        )
        return r.stdout
    except Exception:
        return ""


def _pct_to_rssi(pct: int) -> int:
    """Microsoft formula (inverse): pct = 2*(rssi+100)  →  rssi = pct/2 − 100"""
    return int(pct / 2 - 100)


def _band(channel: int) -> str:
    if channel <= 14:   return "2.4GHz"
    if channel <= 165:  return "5GHz"
    return "6GHz"


def _width_guess(radio: str, band: str) -> str:
    rl = radio.lower()
    if "be" in rl:                              return "160"
    if "ax" in rl:                              return "80"
    if "ac" in rl and band == "5GHz":           return "80"
    if "n"  in rl:                              return "40"
    return "20"


def scan_networks() -> list[dict]:
    """Return sorted list of Wi-Fi networks via netsh wlan show networks mode=Bssid."""
    # Import shared helpers from scanner (they are cross-platform pure-Python)
    from scanner import (
        _assign_color, _update_seen, lookup_vendor,
        _quality, composite_quality_score, _channel_block,
        _seen_ago, _MAX_RATE,
    )

    output = _netsh("show", "networks", "mode=Bssid")
    if not output:
        return []

    networks: list[dict] = []

    # Split at each "SSID N :" marker
    for ssid_block in re.split(r"SSID \d+ :", output)[1:]:
        lines = ssid_block.split("\n")
        ssid = lines[0].strip() or "<Hidden>"

        auth = ""
        for line in lines:
            m = re.match(r"\s*Authentication\s*:\s*(.+)", line)
            if m: auth = m.group(1).strip()

        sec = _SEC_MAP.get(auth.lower(), auth or "Unknown")

        # Each BSSID sub-block within this SSID
        for bblock in re.split(r"BSSID \d+ :", ssid_block)[1:]:
            blines = bblock.split("\n")
            bssid = blines[0].strip()

            sig_pct, radio, channel = 0, "", 1
            for line in blines:
                m = re.match(r"\s*Signal\s*:\s*(\d+)%", line)
                if m: sig_pct = int(m.group(1))
                m = re.match(r"\s*Radio type\s*:\s*(.+)", line)
                if m: radio = m.group(1).strip()
                m = re.match(r"\s*Channel\s*:\s*(\d+)", line)
                if m: channel = int(m.group(1))

            if not bssid or sig_pct == 0:
                continue

            rssi  = _pct_to_rssi(sig_pct)
            noise = -95
            snr   = rssi - noise
            band  = _band(channel)
            width = _width_guess(radio, band)
            gen   = _RADIO_GEN.get(radio.lower(), 4)
            mode  = _RADIO_MODE.get(radio.lower(), "b/g/n")

            color_key = bssid if bssid and not bssid.startswith("<") \
                        else f"__h_{channel}_{band}_{width}"
            color  = _assign_color(color_key)
            seen   = _update_seen(bssid or ssid)
            vendor = lookup_vendor(bssid)

            qkey, qlabel, qcolor = _quality(rssi, snr)
            qscore   = composite_quality_score(rssi, snr, noise)
            ch_block = _channel_block(channel, width, band)
            max_rate = _MAX_RATE.get((gen, int(width)))

            networks.append({
                "ssid":    ssid,
                "bssid":   bssid or "<未知>",
                "vendor":  vendor,
                "rssi":    rssi,
                "noise":   noise,
                "snr":     snr,
                "signal_pct": sig_pct,
                "band":    band,
                "channel": channel,
                "width":   width,
                "ch_block": ch_block,
                "mode":    mode,
                "gen":     gen,
                "max_rate": max_rate,
                "security": sec,
                "is_open":  sec == "Open",
                "standard": radio,
                "quality":  qkey,
                "quality_label": qlabel,
                "quality_color": qcolor,
                "quality_score": qscore,
                "station_count":    None,
                "channel_util_pct": None,
                "net_color":  color,
                "first_seen": seen["first_seen"],
                "last_seen":  seen["last_seen"],
                "seen_ago":   _seen_ago(seen["last_seen"]),
                "is_new":     (time.time() - seen["first_seen"]) < 120,
            })

    networks.sort(key=lambda x: x["rssi"], reverse=True)
    return networks


def current_connection() -> dict:
    """Return current Wi-Fi connection info from netsh wlan show interfaces."""
    from scanner import _quality, lookup_vendor

    output = _netsh("show", "interfaces")
    if not output:
        return {"connected": False}

    info: dict = {}
    for line in output.split("\n"):
        for key, pat in [
            ("state",   r"\s*State\s*:\s*(.+)"),
            ("ssid",    r"\s*SSID\s*:\s*(.+)"),
            ("bssid",   r"\s*BSSID\s*:\s*(.+)"),
            ("channel", r"\s*Channel\s*:\s*(\d+)"),
            ("signal",  r"\s*Signal\s*:\s*(\d+)%"),
            ("radio",   r"\s*Radio type\s*:\s*(.+)"),
            ("txrate",  r"\s*Transmit rate.*:\s*([\d.]+)"),
        ]:
            m = re.match(pat, line)
            if m:
                info[key] = m.group(1).strip()

    if info.get("state", "").lower() != "connected":
        return {"connected": False}

    sig_pct = int(info.get("signal", "0"))
    rssi    = _pct_to_rssi(sig_pct)
    noise   = -95
    snr     = rssi - noise
    channel = int(info.get("channel", "1"))
    band    = _band(channel)
    radio   = info.get("radio", "")
    gen     = _RADIO_GEN.get(radio.lower(), 4)
    bssid   = info.get("bssid", "")

    qkey, qlabel, qcolor = _quality(rssi, snr)

    return {
        "connected":  True,
        "ssid":       info.get("ssid", ""),
        "bssid":      bssid,
        "vendor":     lookup_vendor(bssid),
        "rssi":       rssi,
        "noise":      noise,
        "snr":        snr,
        "signal_pct": sig_pct,
        "band":       band,
        "channel":    channel,
        "width":      _width_guess(radio, band),
        "tx_rate":    float(info.get("txrate", 0)),
        "standard":   radio,
        "gen":        gen,
        "quality":    qkey,
        "quality_label": qlabel,
        "quality_color": qcolor,
    }


def get_supported_bands() -> list[str]:
    nets  = scan_networks()
    bands = {n["band"] for n in nets}
    return sorted(bands) if bands else ["2.4GHz", "5GHz"]
