"""
Wi-Fi Scanner — cross-platform
  macOS  : CoreWLAN via PyObjC
  Windows: netsh wlan (scanner_windows.py)
Supports 2.4 / 5 / 6 GHz, Wi-Fi 4/5/6/6E/7 detection
"""
import platform
import struct
import time
import threading
from typing import Optional

_PLATFORM = platform.system()   # "Darwin" | "Windows" | "Linux"
_selected_iface: str = ""       # "" = use system default
from collections import deque

# ─── OUI vendor lookup (lazy-loaded) ───────────────────────────────────────
_oui: Optional[object] = None
_oui_lock = threading.Lock()

def _get_oui():
    global _oui
    if _oui is not None:
        return _oui
    with _oui_lock:
        if _oui is None:
            try:
                from mac_vendor_lookup import MacLookup
                _oui = MacLookup()
            except Exception:
                _oui = False
    return _oui

def lookup_vendor(bssid: str) -> str:
    """Return vendor name for a BSSID, e.g. 'Apple, Inc.'"""
    if not bssid or ":" not in bssid or bssid.startswith("<"):
        return ""
    try:
        m = _get_oui()
        if not m:
            return ""
        return m.lookup(bssid) or ""
    except Exception:
        return ""


# ─── CoreWLAN constants ────────────────────────────────────────────────────
BAND_MAP  = {1: "2.4GHz", 2: "5GHz", 3: "6GHz"}
WIDTH_MAP = {0: "20", 1: "40", 2: "80", 3: "160", 4: "80+80", 5: "320"}
SEC_MAP = {
    0: "Open", 1: "WEP", 2: "WPA Personal", 3: "WPA/WPA2",
    4: "WPA2", 5: "Personal", 6: "Dynamic WEP",
    7: "WPA Enterprise", 8: "WPA/WPA2 Enterprise", 9: "WPA2 Enterprise",
    10: "Enterprise", 11: "WPA3", 12: "WPA3 Enterprise",
    13: "WPA3 Transition", 14: "OWE", 15: "OWE Transition",
}
# fastestSupportedPHYMode() enum values (kCWPHYMode*)
PHY_MAP = {
    0: "Unknown", 1: "Wi-Fi 1 (802.11a)", 2: "Wi-Fi 1 (802.11b)",
    3: "Wi-Fi 2 (802.11g)", 4: "Wi-Fi 4 (802.11n)",
    5: "Wi-Fi 5 (802.11ac)", 6: "Wi-Fi 6 (802.11ax)",
    7: "Wi-Fi 7 (802.11be)",
}

# Max theoretical data rate (Mbps) by (phy_num, width_str)
# Wi-Fi 4: MCS15 2SS  |  Wi-Fi 5: VHT MCS9 2SS  |  Wi-Fi 6: HE MCS11 1SS  |  Wi-Fi 7: EHT MCS13 1SS
_MAX_RATE: dict[tuple, float] = {
    (4, "20"): 144.4,  (4, "40"): 300.0,
    (5, "20"): 87.0,   (5, "40"): 200.0,  (5, "80"): 867.0,  (5, "160"): 1733.0,
    (6, "20"): 143.4,  (6, "40"): 286.8,  (6, "80"): 600.4,  (6, "160"): 1200.9, (6, "80+80"): 1200.9,
    (7, "80"): 720.6,  (7, "160"): 1441.2,(7, "320"): 2882.4,
}

def _mode_string(band: str, phy_num: int) -> str:
    """Return 802.11 protocol mode string, e.g. 'b/g/n/ax'."""
    if band == "2.4GHz":
        if phy_num >= 7: return "b/g/n/ax/be"
        if phy_num >= 6: return "b/g/n/ax"
        if phy_num >= 4: return "b/g/n"
        if phy_num == 3: return "b/g"
        return "b"
    if band == "5GHz":
        if phy_num >= 7: return "a/n/ac/ax/be"
        if phy_num >= 6: return "a/n/ac/ax"
        if phy_num >= 5: return "a/n/ac"
        if phy_num >= 4: return "a/n"
        return "a"
    if band == "6GHz":
        if phy_num >= 7: return "ax/be"
        return "ax"
    return ""

def _seen_ago(ts: float) -> str:
    diff = time.time() - ts
    if diff < 5:    return "Just now"
    if diff < 60:   return f"{int(diff)} sec ago"
    if diff < 3600: return f"{int(diff/60)} min ago"
    return f"{int(diff/3600)} hr ago"

# ─── Per-network color palette (14 distinct colors) ──────────────────────
_COLORS = [
    "#3b82f6","#22c55e","#f59e0b","#ec4899","#8b5cf6",
    "#14b8a6","#f97316","#6366f1","#84cc16","#06b6d4",
    "#a855f7","#ef4444","#10b981","#f43f5e",
]
_bssid_colors: dict[str, str] = {}
_color_idx: int = 0
_color_lock = threading.Lock()

def _assign_color(bssid: str) -> str:
    global _color_idx
    with _color_lock:
        if bssid not in _bssid_colors:
            _bssid_colors[bssid] = _COLORS[_color_idx % len(_COLORS)]
            _color_idx += 1
        return _bssid_colors[bssid]


# ─── First/last seen tracking ─────────────────────────────────────────────
_seen: dict[str, dict] = {}  # bssid → {first_seen, last_seen, count}

def _update_seen(bssid: str) -> dict:
    now = time.time()
    if bssid not in _seen:
        _seen[bssid] = {"first_seen": now, "last_seen": now, "count": 1}
    else:
        _seen[bssid]["last_seen"] = now
        _seen[bssid]["count"]    += 1
    return _seen[bssid]


# ─── Enterprise signal thresholds ─────────────────────────────────────────
THRESH_EXCELLENT = (-55, 30)
THRESH_GOOD      = (-65, 25)
THRESH_FAIR      = (-70, 20)
THRESH_POOR      = (-75, 15)
THRESH_DATA_MIN  = -65
THRESH_VOICE_MIN = -67
THRESH_DEAD_ZONE = -70
THRESH_ROAM_LOW  = -72


def composite_quality_score(rssi: int, snr: int, noise: int = -95) -> int:
    """
    Weighted composite score 0–100 for Wi-Fi quality.
    RSSI: 50%, SNR: 30%, noise floor: 20%
    Based on enterprise MOS-like scoring.
    """
    rssi_norm  = max(0.0, min(1.0, (rssi + 100) / 70))   # -100→0, -30→1
    snr_norm   = max(0.0, min(1.0, snr / 40))             # 0→0, 40→1
    noise_norm = max(0.0, min(1.0, (noise + 95) / 35))    # -95→1(good), -60→0(bad) → invert
    noise_norm = 1.0 - noise_norm
    score = 0.5 * rssi_norm + 0.3 * snr_norm + 0.2 * noise_norm
    return int(round(score * 100))


def _quality(rssi: int, snr: int) -> tuple[str, str, str]:
    """Returns (key, label_zh, color_hex)"""
    if rssi >= THRESH_EXCELLENT[0] and snr >= THRESH_EXCELLENT[1]:
        return "excellent", "優異", "#22c55e"
    if rssi >= THRESH_GOOD[0] and snr >= THRESH_GOOD[1]:
        return "good", "良好", "#84cc16"
    if rssi >= THRESH_FAIR[0] and snr >= THRESH_FAIR[1]:
        return "fair", "普通", "#eab308"
    if rssi >= THRESH_POOR[0] and snr >= THRESH_POOR[1]:
        return "poor", "較弱", "#f97316"
    return "bad", "信號差", "#ef4444"


def _infer_standard(band: str, width: str, rssi: int) -> str:
    if band == "6GHz":
        if width in ("160", "320", "80+80"):
            return "Wi-Fi 7 (802.11be)*"
        return "Wi-Fi 6E (802.11ax)"
    if band == "5GHz":
        if width in ("80", "80+80", "160"):
            return "Wi-Fi 5/6 (802.11ac/ax)"
        if width == "40":
            return "Wi-Fi 4/5 (802.11n/ac)"
        return "Wi-Fi 4+ (802.11n+)"
    if width == "40":
        return "Wi-Fi 4/6 (802.11n/ax)"
    return "Wi-Fi 4 (802.11n)"


# ─── BSS Load IE (tag 11) parser ─────────────────────────────────────────
def _parse_bss_load(ie_data: bytes) -> dict:
    """
    Parse 802.11 BSS Load Information Element (tag 11).
    Returns: station_count (int), channel_utilization_pct (float 0-100)
    """
    if not ie_data:
        return {}
    i = 0
    result = {}
    while i < len(ie_data) - 1:
        tag_id  = ie_data[i]
        tag_len = ie_data[i + 1]
        if i + 2 + tag_len > len(ie_data):
            break
        tag_data = ie_data[i + 2: i + 2 + tag_len]
        if tag_id == 11 and tag_len >= 5:
            # BSS Load IE: 2B station count + 1B channel utilization + 2B available capacity
            station_count = struct.unpack_from("<H", tag_data, 0)[0]
            channel_util  = tag_data[2]
            result["station_count"]    = station_count
            result["channel_util_pct"] = round(channel_util / 255 * 100, 1)
        i += 2 + tag_len
    return result


def _parse_network(net) -> Optional[dict]:
    try:
        ch    = net.wlanChannel()
        band  = BAND_MAP.get(int(ch.channelBand()), "Unknown")
        width = WIDTH_MAP.get(int(ch.channelWidth()), "20")
        chan  = int(ch.channelNumber())
        rssi  = int(net.rssiValue())
        noise = int(net.noiseMeasurement())
        snr   = rssi - noise

        # Security
        sec_raw = net.strongestSupportedSecurity()
        sec     = SEC_MAP.get(int(sec_raw) if sec_raw is not None else 0, "Unknown")

        # PHY standard
        phy_raw = net.fastestSupportedPHYMode()
        phy_num = int(phy_raw) if phy_raw is not None else 0
        std     = PHY_MAP.get(phy_num, None) or _infer_standard(band, width, rssi)
        if band == "6GHz" and phy_num < 6:
            std = "Wi-Fi 6E (802.11ax)"

        # BSS Load IE (station count + channel utilization)
        bss_load = {}
        try:
            ie_raw = net.informationElementData()
            if ie_raw:
                bss_load = _parse_bss_load(bytes(ie_raw))
        except Exception:
            pass

        bssid = net.bssid() or ""
        ssid  = net.ssid() or "<Hidden>"

        # Assign color — use BSSID when available; fall back to channel fingerprint
        # so hidden networks on different channels get distinct colors
        color_key = bssid if bssid else f"__h_{chan}_{band}_{width}"
        color   = _assign_color(color_key)
        seen    = _update_seen(bssid or ssid)
        vendor  = lookup_vendor(bssid)

        qkey, qlabel, qcolor = _quality(rssi, snr)
        qscore  = composite_quality_score(rssi, snr, noise)
        ch_block = _channel_block(chan, width, band)

        # Signal as 0-100% (Microsoft formula: 2*(dBm+100) clamped)
        signal_pct = max(0, min(100, 2 * (rssi + 100)))

        # Mode string, generation number, max theoretical rate
        mode_str  = _mode_string(band, phy_num)
        gen_num   = phy_num if phy_num >= 4 else (3 if phy_num == 3 else 1)
        max_rate  = _MAX_RATE.get((phy_num, width))

        return {
            "ssid":    ssid,
            "bssid":   bssid or "<需定位授權>",
            "vendor":  vendor,
            "rssi":    rssi,
            "noise":   noise,
            "snr":     snr,
            "signal_pct": signal_pct,
            "band":    band,
            "channel": chan,
            "width":   width,
            "ch_block": ch_block,
            "mode":    mode_str,
            "gen":     gen_num,
            "max_rate": max_rate,
            "security": sec,
            "is_open":  sec == "Open",
            "standard": std,
            "quality": qkey,
            "quality_label": qlabel,
            "quality_color": qcolor,
            "quality_score": qscore,
            "station_count":    bss_load.get("station_count"),
            "channel_util_pct": bss_load.get("channel_util_pct"),
            "net_color":  color,
            "first_seen": seen["first_seen"],
            "last_seen":  seen["last_seen"],
            "seen_ago":   _seen_ago(seen["last_seen"]),
            "is_new":     (time.time() - seen["first_seen"]) < 120,
        }
    except Exception:
        return None


def _channel_block(chan: int, width: str, band: str) -> list[int]:
    """
    Return [first_subchannel, last_subchannel] for bandwidth-aware channel chart.
    E.g., 80 MHz at ch36 → [36, 48] (36,40,44,48).
    """
    if band == "2.4GHz":
        half = {"20": 0, "40": 2}.get(width, 0)
        return [max(1, chan - half), min(14, chan + half)]

    if band == "5GHz":
        # 5GHz 80 MHz blocks (UNII-1/2/3)
        blocks_80 = [
            (36, 48), (52, 64), (100, 112), (116, 128),
            (132, 144), (149, 161),
        ]
        blocks_160 = [(36, 64), (100, 128), (132, 160), (149, 177)]
        if width in ("80", "80+80"):
            for s, e in blocks_80:
                if s <= chan <= e:
                    return [s, e]
        elif width == "160":
            for s, e in blocks_160:
                if s <= chan <= e:
                    return [s, e]
        elif width == "40":
            grp = [(chan // 8) * 8, (chan // 8) * 8 + 4]
            return [min(grp), max(grp) + 4]
        return [chan, chan]

    if band == "6GHz":
        half = {"20": 0, "40": 4, "80": 8, "160": 16, "320": 32}.get(width, 0)
        return [max(1, chan - half), min(233, chan + half)]

    return [chan, chan]


def get_interfaces() -> list[dict]:
    """Return all available Wi-Fi interfaces."""
    if _PLATFORM == "Windows":
        import re, subprocess
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                             capture_output=True, text=True, errors="replace").stdout
        names = re.findall(r"Name\s*:\s*(.+)", out)
        return [{"name": n.strip(), "active": True} for n in names] or \
               [{"name": "Wi-Fi", "active": True}]
    try:
        from CoreWLAN import CWWiFiClient
        client = CWWiFiClient.sharedWiFiClient()
        result = []
        default = client.interface()
        default_name = default.interfaceName() if default else ""
        for iface in (client.interfaces() or [default]):
            name = iface.interfaceName()
            result.append({
                "name": name,
                "active": bool(iface.serviceActive()),
                "default": name == default_name,
            })
        return result
    except Exception:
        return [{"name": "en0", "active": True, "default": True}]


def set_interface(name: str) -> bool:
    """Select which Wi-Fi interface to use for scanning."""
    global _selected_iface
    _selected_iface = name
    return True


def _get_iface():
    """Return the CoreWLAN interface to use (selected or default)."""
    from CoreWLAN import CWWiFiClient
    client = CWWiFiClient.sharedWiFiClient()
    if _selected_iface:
        for iface in (client.interfaces() or []):
            if iface.interfaceName() == _selected_iface:
                return iface
    return client.interface()


def scan_networks() -> list[dict]:
    """Return sorted list of visible Wi-Fi networks (cross-platform)."""
    if _PLATFORM == "Windows":
        from scanner_windows import scan_networks as _win
        return _win()
    # macOS — CoreWLAN
    try:
        iface = _get_iface()
        if iface is None:
            return []
        nets, err = iface.scanForNetworksWithName_error_(None, None)
        if err or not nets:
            return []
        results = [r for n in nets if (r := _parse_network(n))]
        results.sort(key=lambda x: x["rssi"], reverse=True)
        return results
    except ImportError:
        return _demo_networks()
    except Exception as e:
        return [{"_error": str(e)}]


def get_supported_bands() -> list[str]:
    """Return list of bands supported by the Wi-Fi adapter."""
    if _PLATFORM == "Windows":
        from scanner_windows import get_supported_bands as _win
        return _win()
    try:
        iface = _get_iface()
        if iface is None:
            return ["2.4GHz", "5GHz"]
        bands = set()
        for ch in iface.supportedWLANChannels():
            b = BAND_MAP.get(int(ch.channelBand()))
            if b:
                bands.add(b)
        return sorted(bands)
    except Exception:
        return ["2.4GHz", "5GHz"]


def current_connection() -> dict:
    """Return current connection details (cross-platform)."""
    if _PLATFORM == "Windows":
        from scanner_windows import current_connection as _win
        return _win()
    try:
        iface = _get_iface()
        if iface is None:
            return {"connected": False}

        ch    = iface.wlanChannel()
        band  = BAND_MAP.get(int(ch.channelBand()), "Unknown") if ch else "Unknown"
        width = WIDTH_MAP.get(int(ch.channelWidth()), "20") if ch else "20"
        chan  = int(ch.channelNumber()) if ch else 0

        rssi_raw  = iface.rssiValue()
        noise_raw = iface.noiseMeasurement()
        rssi  = int(rssi_raw)  if rssi_raw  is not None else -100
        noise = int(noise_raw) if noise_raw is not None else -100

        if rssi == -100 and noise == -100:
            return {"connected": False}

        snr   = rssi - noise
        tx_raw = iface.transmitRate()
        txr   = round(float(tx_raw), 1) if tx_raw else 0.0

        sp   = _system_profiler_info()
        ssid = iface.ssid() or sp.get("ssid") or "<需定位服務授權>"
        std  = sp.get("phy") or _infer_standard(band, width, rssi)

        bssid  = iface.bssid() or sp.get("bssid", "")
        vendor = lookup_vendor(bssid)
        qkey, qlabel, qcolor = _quality(rssi, snr)
        qscore = composite_quality_score(rssi, snr, noise)

        return {
            "connected": True,
            "ssid":    ssid,
            "bssid":   bssid,
            "vendor":  vendor,
            "rssi":    rssi,
            "noise":   noise,
            "snr":     snr,
            "tx_rate": sp.get("rate") or txr,
            "band":    band,
            "channel": chan,
            "width":   width,
            "standard": std,
            "quality": qkey,
            "quality_label": qlabel,
            "quality_color": qcolor,
            "quality_score": qscore,
        }
    except ImportError:
        return _demo_current()
    except Exception as e:
        return {"connected": False, "error": str(e)}


def _system_profiler_info() -> dict:
    import subprocess, json as _json
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPAirPortDataType", "-json"],
            timeout=5, stderr=subprocess.DEVNULL, text=True
        )
        d    = _json.loads(out)
        aps  = d.get("SPAirPortDataType", [{}])[0]
        for iface in aps.get("spairport_airport_interfaces", []):
            net = iface.get("spairport_current_network_information", {})
            if net:
                phy_map = {
                    "802.11a": "Wi-Fi 1 (802.11a)", "802.11b": "Wi-Fi 1 (802.11b)",
                    "802.11g": "Wi-Fi 2 (802.11g)", "802.11n": "Wi-Fi 4 (802.11n)",
                    "802.11ac": "Wi-Fi 5 (802.11ac)", "802.11ax": "Wi-Fi 6 (802.11ax)",
                    "802.11be": "Wi-Fi 7 (802.11be)",
                }
                ssid    = net.get("_name", "")
                phy_raw = net.get("spairport_network_phymode", "")
                rate    = net.get("spairport_network_rate", 0)
                info    = {}
                if ssid and ssid != "<redacted>":
                    info["ssid"] = ssid
                if phy_raw:
                    info["phy"] = phy_map.get(phy_raw, phy_raw)
                if rate:
                    info["rate"] = float(rate)
                return info
        return {}
    except Exception:
        return {}


# ─── Live monitor ──────────────────────────────────────────────────────────
class SignalMonitor:
    def __init__(self, target_ssid: str = None, interval: float = 2.0, max_pts: int = 120):
        self.target_ssid = target_ssid
        self.interval    = interval
        self.max_pts     = max_pts
        self._lock       = threading.Lock()
        self._data: list[dict] = []
        self._running    = False
        self._thread: threading.Thread = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused by default

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def _loop(self):
        while self._running:
            self._pause_event.wait()
            pt = current_connection()
            if pt.get("connected") and (
                self.target_ssid is None or pt.get("ssid") == self.target_ssid
            ):
                pt["ts"] = time.time()
                with self._lock:
                    self._data.append(pt)
                    if len(self._data) > self.max_pts:
                        self._data = self._data[-self.max_pts:]
            time.sleep(self.interval)

    def get_data(self) -> list[dict]:
        with self._lock:
            return list(self._data)

    def clear(self):
        with self._lock:
            self._data.clear()


# ─── Network performance tester ───────────────────────────────────────────
def test_network_performance(target_host: str = "8.8.8.8",
                              dns_host: str = "8.8.8.8",
                              samples: int = 5) -> dict:
    """
    Test network performance without iperf3 server:
    - ICMP ping latency + jitter + packet loss
    - DNS resolution latency
    - HTTP TTFB latency
    """
    result = {
        "ping_ms": None, "ping_jitter_ms": None, "ping_loss_pct": None,
        "dns_ms": None, "http_ms": None, "errors": []
    }

    # ── ICMP ping ──────────────────────────────────────────────────────────
    try:
        from ping3 import ping
        rtts = []
        losses = 0
        for _ in range(samples):
            t = ping(target_host, timeout=2, unit="ms")
            if t is None:
                losses += 1
            else:
                rtts.append(t)
        if rtts:
            result["ping_ms"] = round(sum(rtts) / len(rtts), 2)
            if len(rtts) > 1:
                mean = result["ping_ms"]
                variance = sum((x - mean)**2 for x in rtts) / len(rtts)
                result["ping_jitter_ms"] = round(variance**0.5, 2)
        result["ping_loss_pct"] = round(losses / samples * 100, 1)
    except Exception as e:
        result["errors"].append(f"ping: {e}")

    # ── DNS latency ────────────────────────────────────────────────────────
    try:
        import dns.resolver as _dns
        import time as _t
        resolver = _dns.Resolver()
        resolver.nameservers = [dns_host]
        resolver.timeout = resolver.lifetime = 3.0
        t0 = _t.perf_counter()
        resolver.resolve("www.google.com", "A")
        result["dns_ms"] = round((_t.perf_counter() - t0) * 1000, 2)
    except Exception as e:
        result["errors"].append(f"dns: {e}")

    # ── HTTP TTFB ──────────────────────────────────────────────────────────
    try:
        import urllib.request, time as _t
        t0 = _t.perf_counter()
        req = urllib.request.urlopen("https://1.1.1.1", timeout=5)
        req.read(1)
        req.close()
        result["http_ms"] = round((_t.perf_counter() - t0) * 1000, 2)
    except Exception as e:
        result["errors"].append(f"http: {e}")

    # ── Quality assessment ─────────────────────────────────────────────────
    if result["ping_ms"] is not None:
        p = result["ping_ms"]
        if p <= 10:   result["ping_grade"] = "優異"
        elif p <= 30: result["ping_grade"] = "良好"
        elif p <= 80: result["ping_grade"] = "普通"
        else:         result["ping_grade"] = "較差"
    if result["dns_ms"] is not None:
        d = result["dns_ms"]
        if d <= 20:   result["dns_grade"] = "優異"
        elif d <= 50: result["dns_grade"] = "良好"
        elif d <= 150:result["dns_grade"] = "普通"
        else:         result["dns_grade"] = "較差"

    return result


# ─── Demo data ─────────────────────────────────────────────────────────────
def _demo_networks() -> list[dict]:
    import random
    entries = [
        ("OfficeNet-5G",  "a0:b1:c2:d3:e4:f5", "5GHz",   36,  "80",  "WPA2"),
        ("OfficeNet-2G",  "a0:b1:c2:d3:e4:f6", "2.4GHz",  6,  "20",  "WPA2"),
        ("Neighbor-A",    "b1:c2:d3:e4:f5:06", "2.4GHz",  6,  "20",  "WPA2"),
        ("Corp-WiFi6E",   "d3:e4:f5:06:17:28", "6GHz",   37, "160",  "WPA3 Enterprise"),
        ("Guest-2G",      "f5:06:17:28:39:4a", "2.4GHz",  1,  "20",  "Open"),
        ("CCTV-5G",       "28:39:4a:5b:6c:7d", "5GHz",  100,  "20",  "WPA2"),
    ]
    result = []
    for ssid, bssid, band, chan, width, sec in entries:
        rssi = random.randint(-78, -42)
        noise = random.randint(-95, -85)
        snr   = rssi - noise
        qkey, qlabel, qcolor = _quality(rssi, snr)
        result.append({
            "ssid": ssid, "bssid": bssid, "vendor": lookup_vendor(bssid),
            "rssi": rssi, "noise": noise, "snr": snr, "band": band,
            "channel": chan, "width": width, "ch_block": _channel_block(chan, width, band),
            "security": sec, "standard": _infer_standard(band, width, rssi),
            "quality": qkey, "quality_label": qlabel, "quality_color": qcolor,
            "quality_score": composite_quality_score(rssi, snr, noise),
            "station_count": random.randint(0, 25), "channel_util_pct": random.randint(10, 75),
            "net_color": _assign_color(bssid), "first_seen": time.time() - random.randint(0,300),
            "last_seen": time.time(), "is_new": False,
        })
    result.sort(key=lambda x: x["rssi"], reverse=True)
    return result


def _demo_current() -> dict:
    rssi, noise = -58, -92
    snr = rssi - noise
    qkey, qlabel, qcolor = _quality(rssi, snr)
    return {
        "connected": True, "ssid": "OfficeNet-5G",
        "bssid": "a0:b1:c2:d3:e4:f5", "vendor": "Demo Vendor",
        "rssi": rssi, "noise": noise, "snr": snr, "tx_rate": 780.0,
        "band": "5GHz", "channel": 36, "width": "80",
        "standard": "Wi-Fi 5/6 (802.11ac/ax)",
        "quality": qkey, "quality_label": qlabel, "quality_color": qcolor,
        "quality_score": composite_quality_score(rssi, snr, noise),
    }
