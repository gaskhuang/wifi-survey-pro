"""
lan_scanner.py — 有線 LAN 盤查引擎（NetInspect Pro）

設計目標：跨平台、免額外硬相依、免 root 即可用。
方法：
  1. ping sweep 整個網段（優先免權限 ICMP datagram socket 一次送完，
     失敗才退回逐台 ping 子程序）→ 作業系統會為每個目標做 ARP 解析，
     因此即使目標封鎖 ICMP，也會進入本機 ARP 快取。
  2. 讀 `arp -an` → 取得每個 L2 可達主機的 MAC（-n 不反解，避免逐筆 PTR 拖慢/逾時）。
  3. MAC → OUI 廠牌（mac-vendor-lookup，離線快取）。
  4. 常見埠掃描（socket connect）→ 服務指紋 → 設備類型啟發式判斷。
  5. 主機名解析（反向 DNS，常駐背景池 + 快取 + 整體期限，不拖住掃描）。

進階指紋（scapy ARP 送包、mDNS、SNMP、UPnP）留待 P1.1 增強，本模組已預留掛點。
"""
import io
import os
import errno
import csv
import re
import time
import queue
import select
import socket
import struct
import threading
import ipaddress
import itertools
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

MAX_HOSTS = 1024               # 單次掃描主機數上限
MAX_ADDRS = MAX_HOSTS + 2      # 位址數上限＝可用主機上限＋網路/廣播位址，避免「接受卻靜默截斷」


def is_valid_ipv4(s: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except (ValueError, TypeError):
        return False


def is_private_ipv4(s: str) -> bool:
    """合法 IPv4 且屬私網/loopback（限制掃描範圍在區網內，避免對外濫用）。"""
    try:
        a = ipaddress.ip_address(s)
        return isinstance(a, ipaddress.IPv4Address) and (a.is_private or a.is_loopback)
    except (ValueError, TypeError):
        return False

import platform_caps as caps


# ── 系統指令絕對路徑（打包 / GUI 啟動時 PATH 常缺 /usr/sbin，需絕對路徑）──────────
def _bin(name: str) -> str:
    for p in (f"/usr/sbin/{name}", f"/sbin/{name}",
              f"/usr/bin/{name}", f"/bin/{name}"):
        if os.path.exists(p):
            return p
    return name          # 後援：交給 PATH（含 Windows）

# ── OUI 廠牌查詢 ─────────────────────────────────────────────────────────────
# 註：mac-vendor-lookup 頂層會連帶 import aiohttp/aiofiles（冷啟 ~80-130ms），
#     而正常情況 OUI 一律讀打包的 data/oui.txt，根本用不到它。
#     因此延後到「缺打包檔」的後援分支內才 import + 建構，省下每次啟動成本。
_oui_ready = False
_OUI = {}          # {6-hex-prefix(str): vendor(str)}，供多執行緒純字典查詢
_OUI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oui.txt")


def _ensure_oui():
    """載入 OUI 廠牌資料庫成純字典（thread-safe，供多執行緒 dict.get）。
    優先讀「打包的離線資料庫」data/oui.txt（確定性、免網路、商用離線可用）；
    缺檔時後援用 mac-vendor-lookup（首次需連網一次）。"""
    global _oui_ready, _OUI
    if _oui_ready:
        return
    # 1) 打包離線資料庫（首選）
    try:
        if os.path.exists(_OUI_FILE):
            d = {}
            with open(_OUI_FILE, encoding="utf-8") as f:
                for line in f:
                    p, _, v = line.rstrip("\n").partition("\t")
                    if p:
                        d[p.strip().upper()] = v
            if d:
                _OUI = d
                _oui_ready = True
                return
    except Exception:
        pass
    # 2) 後援：mac-vendor-lookup（僅在缺打包檔時才 import + 建構，避免拖慢啟動；
    #    首次需連網一次，之後走記憶體查詢）
    try:
        from mac_vendor_lookup import MacLookup   # 延遲 import（含 aiohttp）
        _mac = MacLookup()
        try:
            _mac.lookup("3c:22:fb:00:00:00")
        except Exception:
            _mac.update_vendors()
        pref = getattr(getattr(_mac, "async_lookup", None), "prefixes", {}) or {}
        _OUI = {
            (k.decode() if isinstance(k, bytes) else str(k)).upper():
            (v.decode() if isinstance(v, bytes) else str(v))
            for k, v in pref.items()
        }
    except Exception:
        _OUI = {}
    _oui_ready = True


def _vendor(mac: str) -> str:
    if not mac:
        return ""
    prefix = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()[:6]
    return _OUI.get(prefix, "")


# ── 常見埠 → 服務 ────────────────────────────────────────────────────────────────
PORT_SERVICE = {
    21: "FTP", 22: "SSH", 23: "Telnet", 53: "DNS", 80: "HTTP", 88: "Kerberos",
    111: "RPC", 139: "NetBIOS", 143: "IMAP", 161: "SNMP", 443: "HTTPS",
    445: "SMB", 515: "LPD 列印", 548: "AFP", 554: "RTSP", 631: "IPP 列印",
    993: "IMAPS", 1433: "MSSQL", 1900: "UPnP", 3306: "MySQL", 3389: "RDP",
    5000: "UPnP/Synology", 5001: "Synology HTTPS", 5060: "SIP", 5357: "WSD",
    8006: "Proxmox", 8080: "HTTP-alt/QNAP", 8081: "QNAP", 8443: "HTTPS-alt",
    9100: "RAW 列印", 32400: "Plex",
}
# 掃描時實際探測的埠（取常見且能辨識設備類型者）
SCAN_PORTS = [21, 22, 23, 53, 80, 139, 443, 445, 515, 548, 554, 631,
              1433, 3306, 3389, 5000, 5001, 8006, 8080, 8081, 8443, 9100, 32400]

# 廠牌關鍵字 → 設備類型傾向
VENDOR_HINTS = [
    (("synology", "qnap", "western digital", "netgear readynas", "asustor",
      "drobo", "buffalo"), "NAS"),
    (("ubiquiti", "aruba", "ruckus", "mist", "aerohive", "engenius",
      "cambium", "tp-link", "mercusys"), "Wi-Fi AP"),
    (("fortinet", "palo alto", "sonicwall", "watchguard", "check point",
      "juniper", "sophos", "zyxel"), "路由器/防火牆"),
    (("cisco", "mikrotik", "draytek", "d-link", "edgecore", "hpe aruba"),
     "路由器/防火牆"),
    (("hewlett", "hp inc", "canon", "epson", "brother", "fuji xerox",
      "xerox", "kyocera", "ricoh", "lexmark", "konica"), "印表機"),
    (("apple",), "Apple 裝置"),
    (("raspberry",), "伺服器/Linux"),
    (("espressif", "tuya", "sonoff", "amazon technologies", "google",
      "nest", "xiaomi", "sonos"), "手機/IoT"),
]


def _type_from(vendor: str, ports: set, hostname: str, is_gateway: bool,
               is_self: bool = False) -> tuple:
    """回傳 (設備類型, 信心)。ports 為已開放埠集合。"""
    v = (vendor or "").lower()
    h = (hostname or "").lower()

    # 本機（執行 App 的這台 Mac）
    if is_self:
        return ("本機 (This Mac)", "high")

    # 印表機 / 掃描器：列印埠最明確
    if ports & {9100, 515, 631}:
        return ("印表機/掃描器", "high")
    # NAS：
    #   · Synology 同時開 5000+5001 才算（避免 macOS 本機 5000 誤判）
    #   · QNAP 走 8080/8081 管理埠 + QNAP 廠牌
    #   · NAS 系廠牌直接判定
    nas_vendor = any(k in v for k in ("synology", "qnap", "asustor",
                                      "western digital", "readynas",
                                      "buffalo", "drobo"))
    if ({5000, 5001} <= ports) or nas_vendor or \
       ("qnap" in v and ports & {8080, 8081}):
        return ("NAS", "high")
    # 閘道通常是路由器/防火牆
    if is_gateway:
        return ("路由器/防火牆", "high")
    # 廠牌關鍵字
    for keys, t in VENDOR_HINTS:
        if any(k in v for k in keys):
            # Apple 需進一步分：有檔案分享埠傾向 Mac，否則可能是手機
            if t == "Apple 裝置":
                if ports & {548, 22, 445, 5000, 88}:
                    return ("Mac", "medium")
                return ("Apple 裝置（Mac/iPhone）", "low")
            conf = "medium" if t in ("Wi-Fi AP", "路由器/防火牆", "NAS") else "medium"
            return (t, conf)
    # 埠指紋
    if 3389 in ports or (445 in ports and 139 in ports):
        return ("PC / Windows", "medium")
    if ports & {445, 139}:
        return ("PC / Windows", "low")
    if 32400 in ports:
        return ("媒體伺服器", "medium")
    if {80, 443} & ports and not ports & {445, 139, 22}:
        return ("網頁設備 / IoT", "low")
    if 22 in ports:
        return ("伺服器 / Linux", "low")
    if "printer" in h or "print" in h:
        return ("印表機/掃描器", "low")
    return ("不明", "low")


# ── 網卡 / 網段列舉 ──────────────────────────────────────────────────────────────
def _primary_ip() -> str:
    """用 UDP 連線技巧取得本機主要對外 IP（不會真的送出封包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _default_gateway() -> str:
    """取得預設閘道 IP（跨平台，best-effort）。"""
    try:
        if caps.is_windows():
            out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r"(?:Default Gateway|預設閘道)[ .]*:\s*([\d.]+)", out)
            return m.group(1) if m else ""
        # macOS / Linux
        out = subprocess.run([_bin("netstat"), "-rn"], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] in ("default", "0.0.0.0"):
                for p in parts[1:]:
                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", p):
                        return p
    except Exception:
        pass
    return ""


def _run_out(args, timeout=3) -> str:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, creationflags=flags).stdout
    except Exception:
        return ""


def _primary_iface() -> str:
    """預設路由所走的介面名（macOS/BSD：route -n get default；Linux：ip route）。"""
    if caps.is_windows():
        return ""
    out = _run_out([_bin("route"), "-n", "get", "default"])
    m = re.search(r"interface:\s*(\S+)", out)
    if m:
        return m.group(1)
    out = _run_out([_bin("ip"), "route", "show", "default"])
    m = re.search(r"\bdev\s+(\S+)", out)
    return m.group(1) if m else ""


def _iface_netmask(iface: str, ip: str) -> str:
    """取得介面的真實子網遮罩（點分十進位），失敗回 ""。
    macOS 先用 ipconfig getoption（DHCP 值），再退回解析 ifconfig；Linux 解析 ip addr。"""
    if caps.is_windows():
        out = _run_out(["ipconfig"], timeout=5)
        # 找到本機 IP 那段之後的第一個遮罩
        idx = out.find(ip) if ip else -1
        if idx >= 0:
            m = re.search(r"(?:Subnet Mask|子網路遮罩)[ .]*:\s*([\d.]+)", out[idx:])
            if m:
                return m.group(1)
        return ""
    if not iface:
        return ""
    if caps.is_macos():
        out = _run_out([_bin("ipconfig"), "getoption", iface, "subnet_mask"]).strip()
        if is_valid_ipv4(out):
            return out
    out = _run_out([_bin("ifconfig"), iface])
    for line in out.splitlines():
        if ip and ip not in line:
            continue
        m = re.search(r"netmask\s+(0x[0-9a-fA-F]{8})", line)          # macOS/BSD
        if m:
            return str(ipaddress.IPv4Address(int(m.group(1), 16)))
        m = re.search(r"(?:netmask|Mask:)\s*(\d+\.\d+\.\d+\.\d+)", line)  # Linux net-tools
        if m:
            return m.group(1)
    out = _run_out([_bin("ip"), "-o", "-f", "inet", "addr", "show", iface])
    m = re.search(re.escape(ip) + r"/(\d+)", out) if ip else None
    if m:
        return str(ipaddress.ip_network(f"0.0.0.0/{m.group(1)}").netmask)
    return ""


def list_interfaces() -> list:
    """回傳可掃描的網段清單：以預設路由介面的「真實遮罩」推導網段（/28 熱點、/22 大網段皆正確）。
    真實網段超過 MAX_HOSTS 台時，改建議本機所在的 /24，並以 real_cidr/real_prefix 註明實際網段。
    取不到遮罩時退回 /24。使用者仍可於 UI 覆寫 CIDR。"""
    ip = _primary_ip()
    result = []
    if ip and ip != "127.0.0.1":
        iface = _primary_iface()
        mask = _iface_netmask(iface, ip)
        real = None
        if mask:
            try:
                real = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
            except ValueError:
                real = None
        item = {"name": "主要網卡", "ip": ip, "iface": iface,
                "netmask": mask or "", "gateway": _default_gateway(), "is_default": True}
        if real is not None and real.prefixlen <= 30 and real.num_addresses <= MAX_ADDRS:
            item["subnet_cidr"] = str(real)
            item["prefix"] = real.prefixlen
        else:
            net = ipaddress.ip_network(ip + "/24", strict=False)
            item["subnet_cidr"] = str(net)
            item["prefix"] = 24
            if real is not None:
                item["real_cidr"] = str(real)
                item["real_prefix"] = real.prefixlen
                item["note"] = (f"實際網段 {real}（{max(real.num_addresses - 2, 1):,} 台主機）"
                                f"超出單次上限 {MAX_HOSTS} 台，先建議掃描本機所在 /24")
            else:
                item["note"] = "無法取得子網遮罩，暫以 /24 推估"
        result.append(item)
    return result


# ── Ping（跨平台單一主機）────────────────────────────────────────────────────────
def _ping_cmd(ip: str, count: int, timeout_ms: int) -> list:
    if caps.is_windows():
        return ["ping", "-n", str(count), "-w", str(timeout_ms), ip]
    if caps.is_macos():
        return [_bin("ping"), "-c", str(count), "-W", str(timeout_ms), ip]
    # linux: -W 為秒
    return [_bin("ping"), "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), ip]


def _ping_once(ip: str, timeout_ms: int = 700) -> bool:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(_ping_cmd(ip, 1, timeout_ms),
                           capture_output=True, text=True,
                           timeout=timeout_ms / 1000 + 1.5, creationflags=flags)
        return r.returncode == 0
    except Exception:
        return False


def ping(ip: str, count: int = 4) -> dict:
    """對單一 IP 做 ping 測試，回傳統計。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(_ping_cmd(ip, count, 1000),
                           capture_output=True, text=True,
                           timeout=count * 1.5 + 4, creationflags=flags)
        out = r.stdout
        rtts = [float(x) for x in re.findall(r"time[=<]\s*([\d.]+)\s*ms", out)]
        # 掉包率
        m = re.search(r"([\d.]+)%\s*(?:packet loss|loss|遺失)", out)
        loss = float(m.group(1)) if m else (0.0 if rtts else 100.0)
        avg = round(sum(rtts) / len(rtts), 1) if rtts else None
        return {"ip": ip, "alive": r.returncode == 0 or bool(rtts),
                "rtt_ms": avg,
                "rtt_min": round(min(rtts), 1) if rtts else None,
                "rtt_max": round(max(rtts), 1) if rtts else None,
                "loss_pct": round(loss, 1), "sent": count,
                "recv": len(rtts), "output": out.strip()}
    except Exception as e:
        return {"ip": ip, "alive": False, "rtt_ms": None, "loss_pct": 100.0,
                "sent": count, "recv": 0, "output": str(e)}


# ── ARP 表解析 ───────────────────────────────────────────────────────────────────
_MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")
_IP_RE  = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")


def _norm_mac(mac: str) -> str:
    parts = re.split(r"[:-]", mac)
    if len(parts) != 6:
        return ""
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return ""


def _read_arp_table() -> dict:
    """回傳 {ip: mac}（讀整份作業系統 ARP 快取，用於發現只在 L2 可達的主機）。"""
    table = {}
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # -n：不反解主機名（arp -a 會逐筆 PTR，慢 PTR 時整份逾時被丟棄）；Windows 無 -n
        args = ["arp", "-a"] if caps.is_windows() else [_bin("arp"), "-an"]
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=8, creationflags=flags).stdout
        for line in out.splitlines():
            macm = _MAC_RE.search(line)
            if not macm:
                continue
            # 優先取括號內 IP（macOS「host (ip) at mac」，避免抓到主機名中的假四段），
            # 否則退回第一個像 IP 的字串（Windows 格式），並一律驗證為合法 IPv4。
            pm = re.search(r"\((\d{1,3}(?:\.\d{1,3}){3})\)", line)
            if pm:
                ip = pm.group(1)
            else:
                im = _IP_RE.search(line)
                ip = im.group(0) if im else ""
            if not is_valid_ipv4(ip):
                continue
            mac = _norm_mac(macm.group(1))
            if not mac or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                continue
            table[ip] = mac
    except Exception:
        pass
    return table


def _arp_lookup(ip: str) -> str:
    """查單一 IP 的 MAC（arp -n <ip>）。比整份快照可靠：主機剛回應 ping，
    ARP 必已解析完成，逐台查詢可避開整份快照的時序/過期問題。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        args = ["arp", "-a", ip] if caps.is_windows() else [_bin("arp"), "-n", ip]
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=3, creationflags=flags).stdout
        m = _MAC_RE.search(out)
        if m:
            mac = _norm_mac(m.group(1))
            if mac and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                return mac
    except Exception:
        pass
    return ""


# ── 埠掃描 ───────────────────────────────────────────────────────────────────────
def _probe_port(ip: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except Exception:
        return False


def _scan_ports(ip: str, ports=None, timeout: float = 0.4) -> list:
    ports = ports or SCAN_PORTS
    open_ports = []
    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as ex:
        futs = {ex.submit(_probe_port, ip, p, timeout): p for p in ports}
        for f in as_completed(futs):
            if f.result():
                open_ports.append(futs[f])
    return sorted(open_ports)


def portscan(ip: str, ports=None) -> dict:
    op = _scan_ports(ip, ports, timeout=0.6)
    return {"ip": ip,
            "open_ports": [{"port": p, "service": PORT_SERVICE.get(p, "")} for p in op]}


# ── 反向 DNS：常駐背景池 + 快取 ─────────────────────────────────────────────────
# gethostbyaddr 無逾時參數（不存在的 PTR 實測 ~20 s），但在 macOS 能拿到 Bonjour/mDNS 名稱，
# 故保留它、改丟到常駐 daemon 執行緒池：掃描只等到整體期限，未完成者主機名留空，
# 查完照樣寫入快取供下次使用。用 daemon 執行緒而非 ThreadPoolExecutor：後者的
# worker 在直譯器結束時會被 join，卡住的 PTR 查詢會拖住 App 關閉。
_RDNS_WORKERS = 32
_RDNS_TTL = 600.0                 # 快取 10 分鐘（含查無結果）
_RDNS_CACHE = {}                  # ip → (name, ts)
_RDNS_PENDING = {}                # ip → threading.Event（查詢中，去重）
_RDNS_LOCK = threading.Lock()
_RDNS_Q = queue.Queue()
_rdns_started = False


def _rdns_worker():
    while True:
        ip = _RDNS_Q.get()
        try:
            name = socket.gethostbyaddr(ip)[0]
        except Exception:
            name = ""
        with _RDNS_LOCK:
            _RDNS_CACHE[ip] = (name, time.time())
            ev = _RDNS_PENDING.pop(ip, None)
        if ev:
            ev.set()


def _rdns_submit(ips) -> dict:
    """送出反解（已快取者略過）；回傳 {ip: Event} 供等待。"""
    global _rdns_started
    now = time.time()
    events = {}
    with _RDNS_LOCK:
        if not _rdns_started:
            for i in range(_RDNS_WORKERS):
                threading.Thread(target=_rdns_worker, name=f"rdns-{i}", daemon=True).start()
            _rdns_started = True
        for ip in ips:
            c = _RDNS_CACHE.get(ip)
            if c and now - c[1] < _RDNS_TTL:
                continue
            ev = _RDNS_PENDING.get(ip)
            if ev is None:
                ev = threading.Event()
                _RDNS_PENDING[ip] = ev
                _RDNS_Q.put(ip)
            events[ip] = ev
    return events


def _rdns_cached(ip: str) -> str:
    with _RDNS_LOCK:
        c = _RDNS_CACHE.get(ip)
    return c[0] if c and time.time() - c[1] < _RDNS_TTL else ""


def _hostnames(ips, deadline: float, should_stop=None, events=None) -> dict:
    """等待反解直到 deadline（time.time() 絕對時間）或取消；回傳 {ip: name}（未完成者為 ""）。"""
    if events is None:
        events = _rdns_submit(ips)
    for ev in events.values():
        while not ev.is_set():
            left = deadline - time.time()
            if left <= 0 or (should_stop and should_stop()):
                return {ip: _rdns_cached(ip) for ip in ips}
            ev.wait(min(0.1, left))
    return {ip: _rdns_cached(ip) for ip in ips}


def _hostname(ip: str, timeout: float = 2.0) -> str:
    return _hostnames([ip], time.time() + timeout).get(ip, "")


# ── 快速 ICMP sweep（免 root 的 SOCK_DGRAM ICMP socket）──────────────────────────
def _icmp_checksum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


_ICMP_IDENT = itertools.count((os.getpid() * 7919) & 0xFFFF)


def _icmp_sweep(ips, on_alive=None, should_stop=None, wait_s: float = 1.1,
                retry_after: float = 0.45):
    """一次對所有 ips 送 ICMP echo，收集回覆。回傳存活 IP 集合；建立 socket 失敗回 None
    （呼叫端退回子程序 sweep）。送出 ICMP 同樣會觸發 ARP，既有 ARP 發現機制不受影響。
      · macOS 收到的資料含 IP 標頭（首 nibble==4 時去掉 IHL*4），Linux 不含
      · Linux 核心會把 identifier 改成 socket 的本地「埠」，兩者都接受
      · retry_after 秒後對尚未回應者補送一輪（降低 Wi-Fi 單包遺失造成的漏掃）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (OSError, AttributeError):
        return None
    try:
        s.setblocking(False)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        ident = next(_ICMP_IDENT) & 0xFFFF                # 每次 sweep 不同，並行 sweep 互不混淆
        idents = {ident}
        index = {ip: i for i, ip in enumerate(ips)}
        alive = set()
        payload = b"NetInspectPro..."

        def _stopped():
            return bool(should_stop and should_stop())

        def _drain():
            while True:
                try:
                    data, addr = s.recvfrom(2048)
                except (BlockingIOError, InterruptedError):
                    return
                except OSError:
                    return
                if data and (data[0] >> 4) == 4:              # macOS：含 IP 標頭
                    data = data[(data[0] & 0x0F) * 4:]
                if len(data) < 8:
                    continue
                typ, _code, _ck, rid, seq = struct.unpack("!BBHHH", data[:8])
                src = addr[0]
                if typ != 0 or rid not in idents or src in alive:
                    continue
                if index.get(src) != seq:                     # 序號與來源位址須對應
                    continue
                alive.add(src)
                if on_alive:
                    on_alive(src)

        def _send(ip):
            seq = index[ip] & 0xFFFF
            hdr = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            pkt = struct.pack("!BBHHH", 8, 0, _icmp_checksum(hdr + payload), ident, seq) + payload
            for _ in range(3):
                try:
                    s.sendto(pkt, (ip, 0))
                    return
                except BlockingIOError:
                    time.sleep(0.002)
                except OSError as e:                          # ENOBUFS → 稍等重試；其餘（如 EHOSTDOWN）略過
                    if e.errno == errno.ENOBUFS:
                        time.sleep(0.005)
                        continue
                    return

        def _round(targets):
            for n, ip in enumerate(targets):
                if _stopped():
                    return False
                _send(ip)
                if n % 32 == 31:                              # 小幅節流並順手收回覆
                    _drain()
                    time.sleep(0.003)
            return True

        if not _round(ips):
            return alive
        try:
            idents.add(s.getsockname()[1] & 0xFFFF)           # Linux：核心改寫後的 identifier
        except OSError:
            pass
        t_retry = time.time() + retry_after
        end = time.time() + wait_s
        retried = False
        while True:
            now = time.time()
            if _stopped() or len(alive) >= len(index):      # 全數回覆即提前結束
                return alive
            if not retried and now >= t_retry:
                retried = True
                if not _round([ip for ip in ips if ip not in alive]):
                    return alive
                end = max(end, time.time() + 0.6)
                continue
            if now >= end:
                break
            r, _, _ = select.select([s], [], [], min(0.05, end - now))
            if r:
                _drain()
        return alive
    finally:
        s.close()


# ── 主掃描流程 ───────────────────────────────────────────────────────────────────
def _pool_run(ex, jobs, should_stop, on_done) -> bool:
    """在 ex 上提交 jobs=[(key, fn, args)]，完成時 on_done(key, future)。
    以 0.1 s 輪詢 should_stop（as_completed 會一直阻塞到下一個完成），取消時
    shutdown(wait=False, cancel_futures=True) 立即返回；回傳 False 表示被取消。"""
    def _cancelled():
        return bool(should_stop and should_stop())

    def _guard(fn, args):
        if _cancelled():                 # 排隊中的工作開跑前先檢查，取消後不再做事
            return None
        return fn(*args)

    pending = {}
    try:
        for key, fn, args in jobs:
            pending[ex.submit(_guard, fn, args)] = key
        while pending:
            if _cancelled():
                return False
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for f in done:
                on_done(pending.pop(f), f)
        return not _cancelled()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


HOSTNAME_DEADLINE = 2.0          # 反解整體等待上限（與埠掃描並行，從開始送出起算）


def scan(subnet_cidr: str, deep: bool = True, progress=None,
         should_stop=None) -> dict:
    """
    掃描一個網段。
      subnet_cidr : 例 "192.168.1.0/24"
      deep        : True 則對存活主機做埠掃描與類型判斷
      progress    : callback(done:int, total:int, host:dict|None)
      should_stop : callable()->bool，回 True 則提前結束（< 1 s 內返回）
    回傳 {subnet, gateway, hosts:[...], summary:{...}, alive_count, total}
    """
    net = ipaddress.ip_network(subnet_cidr, strict=False)
    if net.version != 4:
        raise ValueError("目前僅支援 IPv4 網段掃描")
    if net.num_addresses > MAX_ADDRS:
        raise ValueError(f"網段過大（{net.num_addresses:,} 個位址、{net.num_addresses - 2} 台主機）。"
                         f"請改用 /22 以上（≤{MAX_HOSTS} 台）較小的網段，以免漏掃。")
    # 惰性物化（此處已保證 ≤ MAX_HOSTS，不會發生靜默截斷）
    hosts_iter = list(itertools.islice(net.hosts(), MAX_HOSTS))
    total = len(hosts_iter)
    gateway = _default_gateway()

    def _cancelled():
        return bool(should_stop and should_stop())

    def _partial(hosts):
        summ = {}
        for h in hosts:
            summ[h["type"]] = summ.get(h["type"], 0) + 1
        return {"subnet": str(net), "gateway": gateway, "hosts": hosts,
                "summary": summ, "alive_count": len(hosts), "total": total,
                "deep": deep, "cancelled": True}
    self_ip = _primary_ip()
    _ensure_oui()               # 並發查詢前先預熱 OUI 資料庫

    # 1) ping sweep（同時觸發 ARP 解析）— 每探到一台就即時回報（邊掃邊顯示）
    alive = set()
    ips = [str(ip) for ip in hosts_iter]
    prog_lock = threading.Lock()
    state = {"done": 0}
    if progress:
        progress(0, total, {"_phase": "ping"})

    def _alive_row(ip, done):
        progress(done, total, {
            "ip": ip, "ping_ok": True, "mac": "", "vendor": "掃描中…",
            "type": "掃描中…", "hostname": "", "open_ports": [],
            "services": [], "is_gateway": ip == gateway,
            "confidence": {}, "sources": ["ping"], "partial": True})

    def _advance(to):
        """done 逐一遞增到 to（與舊版「每完成一台呼叫一次」語意相同）。"""
        while state["done"] < to:
            state["done"] += 1
            if progress:
                progress(state["done"], total, None)

    # 1a) 快速路徑：ICMP datagram socket（/24 約 1 s）；done 依等待時間比例推進
    t_ping = time.time()
    est = 1.2 + total * 0.0003                  # 預估 sweep 耗時（送出 + 補送 + 等待）

    def _on_alive(ip):
        with prog_lock:
            alive.add(ip)
            _advance(min(total - 1, int(total * (time.time() - t_ping) / est)))
            if progress:
                _alive_row(ip, state["done"])

    ticker_stop = threading.Event()

    def _ticker():
        while not ticker_stop.wait(0.1):
            with prog_lock:
                _advance(min(total - 1, int(total * (time.time() - t_ping) / est)))

    tk = threading.Thread(target=_ticker, daemon=True)
    tk.start()
    try:
        swept = _icmp_sweep(ips, on_alive=_on_alive, should_stop=should_stop)
    finally:
        ticker_stop.set()
        tk.join()
    if swept is not None:
        with prog_lock:
            _advance(total)
    else:
        # 1b) 後援：逐台 ping 子程序（無法建立 ICMP socket 時，例如部分 Linux 的 ping_group_range）
        def _on_ping(ip, f):
            try:
                ok = f.result()
            except Exception:
                ok = False
            state["done"] += 1
            if ok:
                alive.add(ip)
                if progress:
                    _alive_row(ip, state["done"])
            if progress:
                progress(state["done"], total, None)
        _pool_run(ThreadPoolExecutor(max_workers=64),
                  [(ip, _ping_once, (ip,)) for ip in ips], should_stop, _on_ping)

    if _cancelled():                      # 已取消 → 不進入昂貴的埠掃描/指紋階段
        return _partial([])

    # 2) 讀 ARP 表（包含未回 ICMP 但 L2 可達者；key 已在 _read_arp_table 驗證為合法 IPv4）
    arp = _read_arp_table()
    present = set(alive) | {ip for ip in arp if ipaddress.ip_address(ip) in net}
    if net.prefixlen <= 30:               # 網路/廣播位址不是主機（/31、/32 例外）
        present.discard(str(net.broadcast_address))
        present.discard(str(net.network_address))

    # 3) 對每台主機補齊指紋
    hosts = []
    plist = sorted(present, key=lambda x: tuple(int(o) for o in x.split(".")))

    if _cancelled():
        return _partial([])

    # 3a) 反向 DNS 先送出（常駐池），與埠掃描並行；整體期限 HOSTNAME_DEADLINE
    rdns_deadline = time.time() + HOSTNAME_DEADLINE
    rdns_events = _rdns_submit(plist)

    # 3b) 深度掃描：集中所有 (ip, port) 到單一扁平執行緒池，避免巢狀池造成執行緒爆量
    ports_by_ip = {ip: set() for ip in plist}
    if deep and plist:
        if progress:
            progress(total, total, {"_phase": "portscan", "count": len(plist)})

        def _on_port(key, f):
            try:
                if f.result():
                    ports_by_ip[key[0]].add(key[1])
            except Exception:
                pass
        if not _pool_run(ThreadPoolExecutor(max_workers=100),
                         [((ip, port), _probe_port, (ip, port, 0.4))
                          for ip in plist for port in SCAN_PORTS],
                         should_stop, _on_port):
            return _partial([])

    names = _hostnames(plist, rdns_deadline, should_stop, rdns_events)
    if _cancelled():
        return _partial([])

    def _fingerprint(ip):
        mac = arp.get(ip) or _arp_lookup(ip)      # 整份快照優先，缺則逐台查
        vendor = _vendor(mac)
        hostname = names.get(ip, "")
        ports = ports_by_ip.get(ip, set())        # 埠已於 3b 集中掃完
        dtype, conf = _type_from(vendor, ports, hostname, ip == gateway,
                                 is_self=(ip == self_ip))
        sources = ["ping"] if ip in alive else []
        if mac:
            sources.append("arp")
        if ports:
            sources.append("port")
        return {
            "ip": ip, "mac": mac, "vendor": vendor or "未知",
            "type": dtype, "hostname": hostname,
            "open_ports": sorted(ports),
            "services": [PORT_SERVICE.get(p, str(p)) for p in sorted(ports)],
            "is_gateway": ip == gateway,
            "ping_ok": ip in alive,
            "confidence": {"vendor": "high" if vendor else "low",
                           "type": conf,
                           "model": "low"},
            "sources": sources,
        }

    if progress and plist:
        progress(total, total, {"_phase": "fingerprint", "count": len(plist)})

    def _on_fp(ip, f):
        try:
            h = f.result()
        except Exception:
            return
        if h is None:
            return
        hosts.append(h)
        if progress:
            progress(total, total, h)   # host 逐台回報（補上完整指紋）
    if not _pool_run(ThreadPoolExecutor(max_workers=32),
                     [(ip, _fingerprint, (ip,)) for ip in plist], should_stop, _on_fp):
        hosts.sort(key=lambda h: tuple(int(o) for o in h["ip"].split(".")))
        return _partial(hosts)

    hosts.sort(key=lambda h: tuple(int(o) for o in h["ip"].split(".")))

    # 4) 統計
    summary = {}
    for h in hosts:
        summary[h["type"]] = summary.get(h["type"], 0) + 1

    return {
        "subnet": str(net),
        "gateway": gateway,
        "hosts": hosts,
        "summary": summary,
        "alive_count": len(hosts),
        "total": total,
        "deep": deep,
    }


# ── 匯出 ─────────────────────────────────────────────────────────────────────────
def to_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["IP", "MAC", "廠牌", "設備類型", "主機名", "開放埠",
                "是否閘道", "PING", "來源"])
    for h in result.get("hosts", []):
        w.writerow([h["ip"], h["mac"], h["vendor"], h["type"], h["hostname"],
                    " ".join(str(p) for p in h["open_ports"]),
                    "是" if h["is_gateway"] else "",
                    "OK" if h["ping_ok"] else "",
                    "+".join(h["sources"])])
    return buf.getvalue()


if __name__ == "__main__":
    import json
    ifs = list_interfaces()
    print("介面：", json.dumps(ifs, ensure_ascii=False))
    if ifs:
        r = scan(ifs[0]["subnet_cidr"], deep=True,
                 progress=lambda d, t, h: (
                     print(f"  ✓ {h['ip']:<15} {h['vendor']:<20} {h['type']}")
                     if h else None))
        print(json.dumps(r["summary"], ensure_ascii=False, indent=2))
        print(f"共 {r['alive_count']} 台 / 掃描 {r['total']} 個位址")
