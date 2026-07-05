"""
lan_scanner.py — 有線 LAN 盤查引擎（NetInspect Pro）

設計目標：跨平台、免額外硬相依、免 root 即可用。
方法：
  1. ping sweep 整個網段（並發）→ 作業系統會為每個目標做 ARP 解析，
     因此即使目標封鎖 ICMP，也會進入本機 ARP 快取。
  2. 讀 `arp -a` → 取得每個 L2 可達主機的 MAC。
  3. MAC → OUI 廠牌（mac-vendor-lookup，離線快取）。
  4. 常見埠掃描（socket connect）→ 服務指紋 → 設備類型啟發式判斷。
  5. 主機名解析（反向 DNS）。

進階指紋（scapy ARP 送包、mDNS、SNMP、UPnP）留待 P1.1 增強，本模組已預留掛點。
"""
import io
import os
import csv
import re
import socket
import ipaddress
import itertools
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_HOSTS = 1024          # 單次掃描主機數上限
MAX_ADDRS = 4096          # 拒絕超過此位址數的網段（避免誤掃/資源耗盡）


def is_valid_ipv4(s: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
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

# ── OUI 廠牌查詢（離線快取；首次可能需連網下載一次）─────────────────────────────
try:
    from mac_vendor_lookup import MacLookup
    _MAC = MacLookup()
except Exception:
    _MAC = None

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
    # 2) 後援：mac-vendor-lookup（首次需網路，之後記憶體查詢）
    if _MAC is not None:
        try:
            try:
                _MAC.lookup("3c:22:fb:00:00:00")
            except Exception:
                _MAC.update_vendors()
            pref = getattr(getattr(_MAC, "async_lookup", None), "prefixes", {}) or {}
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


def list_interfaces() -> list:
    """回傳可掃描的網段清單。MVP：以主要 IP 推導 /24（最常見），使用者可於 UI 覆寫 CIDR。"""
    ip = _primary_ip()
    result = []
    if ip and ip != "127.0.0.1":
        net = ipaddress.ip_network(ip + "/24", strict=False)
        result.append({
            "name":        "主要網卡",
            "ip":          ip,
            "subnet_cidr": str(net),
            "gateway":     _default_gateway(),
            "is_default":  True,
        })
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
        out = subprocess.run([_bin("arp"), "-a"], capture_output=True, text=True,
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


def _hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


# ── 主掃描流程 ───────────────────────────────────────────────────────────────────
def scan(subnet_cidr: str, deep: bool = True, progress=None,
         should_stop=None) -> dict:
    """
    掃描一個網段。
      subnet_cidr : 例 "192.168.1.0/24"
      deep        : True 則對存活主機做埠掃描與類型判斷
      progress    : callback(done:int, total:int, host:dict|None)
      should_stop : callable()->bool，回 True 則提前結束
    回傳 {subnet, gateway, hosts:[...], summary:{...}, alive_count, total}
    """
    net = ipaddress.ip_network(subnet_cidr, strict=False)
    if net.version != 4:
        raise ValueError("目前僅支援 IPv4 網段掃描")
    if net.num_addresses > MAX_ADDRS:
        raise ValueError(f"網段過大（{net.num_addresses:,} 個位址）。"
                         f"請改用 /20 以上（≤{MAX_ADDRS}）較小的網段。")
    # 惰性取前 MAX_HOSTS 個，避免超大網段全量物化吃爆記憶體
    hosts_iter = list(itertools.islice(net.hosts(), MAX_HOSTS))
    total = len(hosts_iter)
    gateway = _default_gateway()
    self_ip = _primary_ip()
    _ensure_oui()               # 並發查詢前先預熱 OUI 資料庫

    # 1) 並發 ping sweep（同時觸發 ARP 解析）
    alive = set()
    done = 0
    with ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(_ping_once, str(ip)): str(ip) for ip in hosts_iter}
        for f in as_completed(futs):
            if should_stop and should_stop():
                break
            ip = futs[f]
            done += 1
            try:
                if f.result():
                    alive.add(ip)
            except Exception:
                pass
            if progress:
                progress(done, total, None)

    # 2) 讀 ARP 表（包含未回 ICMP 但 L2 可達者；key 已在 _read_arp_table 驗證為合法 IPv4）
    arp = _read_arp_table()
    present = set(alive) | {ip for ip in arp if ipaddress.ip_address(ip) in net}

    # 3) 對每台主機補齊指紋
    hosts = []
    plist = sorted(present, key=lambda x: tuple(int(o) for o in x.split(".")))

    # 3a) 深度掃描：集中所有 (ip, port) 到單一扁平執行緒池，避免巢狀池造成執行緒爆量
    ports_by_ip = {ip: set() for ip in plist}
    if deep and plist:
        tasks = [(ip, port) for ip in plist for port in SCAN_PORTS]
        with ThreadPoolExecutor(max_workers=100) as ex:
            futs = {ex.submit(_probe_port, ip, port, 0.4): (ip, port)
                    for ip, port in tasks}
            for f in as_completed(futs):
                if should_stop and should_stop():
                    break
                ip, port = futs[f]
                try:
                    if f.result():
                        ports_by_ip[ip].add(port)
                except Exception:
                    pass

    def _fingerprint(ip):
        mac = arp.get(ip) or _arp_lookup(ip)      # 整份快照優先，缺則逐台查
        vendor = _vendor(mac)
        hostname = _hostname(ip)
        ports = ports_by_ip.get(ip, set())        # 埠已於 3a 集中掃完
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

    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = {ex.submit(_fingerprint, ip): ip for ip in plist}
        fdone = 0
        for f in as_completed(futs):
            if should_stop and should_stop():
                break
            try:
                h = f.result()
            except Exception:
                continue
            hosts.append(h)
            fdone += 1
            if progress:
                progress(total, total, h)   # host 逐台回報

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
