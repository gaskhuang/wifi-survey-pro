"""
net_env.py — 網路環境總覽（NetInspect Pro）

工程師到客戶現場，先要知道「這個網路長什麼樣子」：
  · 區域網路：本機 IP / 遮罩、閘道（含廠牌）、DNS、DHCP 伺服器與租約、連線方式
  · 對外網路：公網 IP、ISP、所在地、IPv6 是否可用、是否有強制登入頁（captive portal）
  · NAT 層數：路由前幾跳若有兩台以上私有位址的路由器 → 雙重 NAT；100.64/10 → 電信級 NAT

全部唯讀、只用標準庫與系統內建指令，個別項目失敗不影響其他項目。
"""
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
import urllib.request

import lan_scanner

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _run(args, timeout=4) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ── 區域網路 ──────────────────────────────────────────────────────────────────
def _dhcp(iface: str) -> dict:
    """macOS：ipconfig getpacket <iface> → DHCP 伺服器、租約時間、網域。"""
    out = {}
    txt = _run(["/usr/sbin/ipconfig", "getpacket", iface], 3) if iface else ""
    if not txt:
        return out
    m = re.search(r"server_identifier \(ip\): (\S+)", txt)
    if m:
        out["server"] = m.group(1)
    m = re.search(r"lease_time \(uint32\): (0x[0-9a-fA-F]+|\d+)", txt)
    if m:
        try:
            out["lease_sec"] = int(m.group(1), 0)
        except ValueError:
            pass
    m = re.search(r"domain_name \(string\): (\S+)", txt)
    if m:
        out["domain"] = m.group(1)[:80]
    m = re.search(r"domain_name_server \(ip_mult\): \{([^}]*)\}", txt)
    if m:
        out["dns"] = [x.strip() for x in m.group(1).split(",") if x.strip()][:4]
    return out


def _dns_servers() -> list:
    out = []
    for line in _run(["/usr/sbin/scutil", "--dns"], 3).splitlines():
        m = re.search(r"nameserver\[\d+\]\s*:\s*(\S+)", line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out[:6]


def local() -> dict:
    ip = lan_scanner._primary_ip()
    gw = lan_scanner._default_gateway()
    iface = lan_scanner._primary_iface()
    mask = lan_scanner._iface_netmask(iface, ip) if iface and ip else ""
    cidr = ""
    try:
        if ip and mask:
            cidr = str(ipaddress.ip_network(f"{ip}/{mask}", strict=False))
    except ValueError:
        pass
    gw_mac = lan_scanner._arp_lookup(gw) if gw else ""
    lan_scanner._ensure_oui()
    dhcp = _dhcp(iface)
    return {
        "interface": iface, "link": lan_scanner._link_label(iface) if iface else "",
        "ip": ip, "netmask": mask, "subnet": cidr,
        "apipa": ip.startswith("169.254.") if ip else False,
        "gateway": gw, "gateway_mac": gw_mac,
        "gateway_vendor": lan_scanner._vendor(gw_mac) if gw_mac else "",
        "dns": _dns_servers() or dhcp.get("dns") or [],
        "dhcp": dhcp,
        # DHCP 伺服器不是閘道：可能是獨立 DHCP，也可能是有人私接分享器
        "dhcp_not_gateway": bool(dhcp.get("server") and gw and dhcp["server"] != gw),
    }


# ── 對外網路 ──────────────────────────────────────────────────────────────────
def _fetch(url: str, timeout: float = 5.0, headers: dict | None = None, limit: int = 20000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 NetInspectPro", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def _public_identity() -> dict:
    """公網 IP 與 ISP（Cloudflare；App 的測速也走這家，不另外依賴第三方）。"""
    try:
        d = json.loads(_fetch("https://speed.cloudflare.com/meta",
                              headers={"Referer": "https://speed.cloudflare.com/"}))
        if d.get("clientIp"):
            return {"ip": d["clientIp"], "isp": d.get("asOrganization") or "",
                    "asn": d.get("asn"), "city": d.get("city") or "",
                    "country": d.get("country") or "", "colo": (d.get("colo") or {}).get("iata", "")}
    except Exception:
        pass
    try:                                              # 後援：只有 IP 與國家
        kv = dict(line.split("=", 1) for line in _fetch("https://1.1.1.1/cdn-cgi/trace").splitlines() if "=" in line)
        if kv.get("ip"):
            return {"ip": kv["ip"], "isp": "", "asn": None, "city": "", "country": kv.get("loc", ""),
                    "colo": kv.get("colo", "")}
    except Exception:
        pass
    return {}


def _ipv6_ok() -> bool:
    try:
        with socket.create_connection(("2606:4700:4700::1111", 443), timeout=2.5):
            return True
    except OSError:
        return False


def _captive_portal():
    """回傳 True（有強制登入頁）/ False / None（無法判斷，例如完全沒有對外連線）。"""
    try:
        return "Success" not in _fetch("http://captive.apple.com/hotspot-detect.html", 4.0, limit=4000)
    except Exception:
        return None


def _nat_path(max_hops: int = 5, gateway: str = "") -> dict:
    """看對外路徑的前幾跳：私有位址的路由器有幾層、有沒有電信級 NAT。
    gateway：已知的預設閘道。路由器常限制 traceroute 的回應頻率，第一跳沒回應時用它補上，
    否則少算一層（雙重 NAT 會被誤判成單層）。"""
    # 每一跳探測 2 次：只探 1 次時，路由器偶爾不回應就會漏掉一層（雙重 NAT 被誤判成單層）
    txt = _run(["/usr/sbin/traceroute", "-n", "-m", str(max_hops), "-q", "2", "-w", "1", "1.1.1.1"],
               timeout=max_hops * 2.5 + 3)
    hops = []
    for line in txt.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        ipm = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", m.group(2))
        if not ipm:
            hops.append({"hop": int(m.group(1)), "ip": "", "kind": "無回應"})
            continue
        try:
            ipa = ipaddress.ip_address(ipm.group(1))
        except ValueError:
            continue
        kind = "電信級 NAT" if ipa in _CGNAT else ("私有" if ipa.is_private else "公網")
        ms = re.search(r"([\d.]+) ms", line)
        hops.append({"hop": int(m.group(1)), "ip": ipm.group(1), "kind": kind,
                     "ms": float(ms.group(1)) if ms else None})
    if gateway and _is_private(gateway) and not any(h["ip"] == gateway for h in hops):
        first = {"hop": 1, "ip": gateway, "kind": "私有", "ms": None}
        if hops and hops[0]["hop"] == 1 and not hops[0]["ip"]:
            hops[0] = first                           # 第一跳沒回應 → 就是閘道
        elif not hops or hops[0]["hop"] > 1:
            hops.insert(0, first)
    private = []
    for h in hops:                                    # 從第一跳起連續的私有 / CGNAT 位址
        if h["kind"] in ("私有", "電信級 NAT"):
            private.append(h)
        elif h["kind"] == "公網":
            break
    lan_layers = [h for h in private if h["kind"] == "私有"]
    # 172.16/12 與 10/8 出現在第 2 跳以後，多半是 ISP 的內部骨幹而不是客戶自己的第二台路由器
    customer = [h for i, h in enumerate(lan_layers)
                if i == 0 or ipaddress.ip_address(h["ip"]) in ipaddress.ip_network("192.168.0.0/16")]
    return {
        "hops": hops[:max_hops],
        "private_hops": len(private),
        "double_nat": len(customer) >= 2,
        "cgnat": any(h["kind"] == "電信級 NAT" for h in private) or
                 (len(lan_layers) > len(customer)),        # 前幾跳有 ISP 私有骨幹 → 多半拿不到真正的公網 IP
        "routers": [h["ip"] for h in customer],
    }


def wan(gateway: str = "") -> dict:
    """對外網路資訊；各項並行，整體約 3～6 秒。"""
    box = {}

    def run(key, fn):
        try:
            box[key] = fn()
        except Exception:
            box[key] = None

    jobs = (("identity", _public_identity), ("ipv6", _ipv6_ok),
            ("captive", _captive_portal), ("nat", lambda: _nat_path(gateway=gateway)))
    threads = [threading.Thread(target=run, args=j, daemon=True) for j in jobs]
    for t in threads:
        t.start()
    deadline = time.time() + 14
    for t in threads:
        t.join(timeout=max(0.1, deadline - time.time()))
    ident = box.get("identity") or {}
    nat = box.get("nat") or {}
    return {
        "online": bool(ident.get("ip")),
        "public_ip": ident.get("ip", ""), "isp": ident.get("isp", ""), "asn": ident.get("asn"),
        "city": ident.get("city", ""), "country": ident.get("country", ""),
        "ipv6": bool(box.get("ipv6")),
        "captive_portal": box.get("captive"),
        "nat": nat,
    }


def overview() -> dict:
    """區域網路 + 對外網路 + 判讀出來的注意事項。"""
    box = {}
    gw = lan_scanner._default_gateway()
    t = threading.Thread(target=lambda: box.update(wan=wan(gw)), daemon=True)
    t.start()
    lan = local()
    t.join(timeout=20)
    w = box.get("wan") or {"online": False, "nat": {}}
    return {"lan": lan, "wan": w, "issues": assess(lan, w)}


def _issue(sev, cat, title, problem, rec):
    return {"severity": sev, "category": cat, "icon": "", "title": title,
            "problem": problem, "recommendation": rec}


def assess(lan: dict, w: dict) -> list:
    """把環境資訊轉成工程師看得懂的提醒（格式同優化建議面板）。"""
    out = []
    nat = w.get("nat") or {}
    if lan.get("apipa"):
        out.append(_issue("critical", "DHCP", "沒有拿到 IP（169.254 自動位址）",
                          f"本機位址 {lan.get('ip')} 是自動設定位址，代表 DHCP 沒有回應。",
                          "檢查 DHCP 伺服器是否運作、位址池是否用完、交換器的 VLAN 設定。"))
    if not lan.get("gateway"):
        out.append(_issue("critical", "路由", "沒有預設閘道", "封包無法送出本網段。",
                          "確認 DHCP 有派發閘道，或手動設定正確的閘道位址。"))
    elif not w.get("online"):
        out.append(_issue("critical", "對外", "無法連上網際網路",
                          f"閘道 {lan['gateway']} 存在，但對外查詢失敗。",
                          "檢查數據機 / ISP 線路、閘道的 WAN 狀態與對外防火牆規則。"))
    if w.get("captive_portal"):
        out.append(_issue("warning", "對外", "這個網路有強制登入頁（Captive Portal）",
                          "連外的網頁被導向登入頁，登入前的測速與診斷結果不準。",
                          "先在瀏覽器完成登入，再重新執行診斷。"))
    if nat.get("double_nat"):
        r = " → ".join(nat.get("routers") or [])
        out.append(_issue("warning", "NAT", "雙重 NAT（兩層路由器）",
                          f"對外路徑連續經過兩台私有位址的路由器：{r}。",
                          "會造成 VPN、遠端桌面、視訊通話、連接埠轉送異常。把其中一台改成橋接（AP）模式，"
                          "或在上層路由器把下層設為 DMZ。"))
    if nat.get("cgnat"):
        out.append(_issue("info", "NAT", "ISP 可能使用電信級 NAT（沒有獨立公網 IP）",
                          "路徑前幾跳出現 ISP 的內部位址，客戶端拿到的多半不是真正的公網 IP。",
                          "需要從外部連回來（監視器、NAS、VPN 伺服器）時，要向 ISP 申請固定 / 公網 IP，或改用反向連線服務。"))
    if lan.get("dhcp_not_gateway"):
        out.append(_issue("info", "DHCP", "DHCP 伺服器不是閘道",
                          f"DHCP 由 {lan['dhcp'].get('server')} 發放，閘道是 {lan.get('gateway')}。",
                          "若客戶沒有獨立的 DHCP 伺服器（例如 Windows Server），可能有人私接了分享器，請確認這台設備。"))
    lease = (lan.get("dhcp") or {}).get("lease_sec")
    if lease and lease < 1800:
        out.append(_issue("info", "DHCP", f"DHCP 租約很短（{lease // 60} 分鐘）",
                          "設備會頻繁續租，位址池緊張時容易拿不到 IP。",
                          "一般辦公室建議 8～24 小時；訪客網路可用 1～2 小時。"))
    dns = lan.get("dns") or []
    if dns and all(_is_private(d) for d in dns) and len(dns) == 1:
        out.append(_issue("info", "DNS", "只有一台 DNS 伺服器",
                          f"目前只用 {dns[0]}，它故障時所有設備都會「連不上網」。",
                          "在 DHCP 加上第二台 DNS（例如 1.1.1.1 或 8.8.8.8）作為備援。"))
    if not out:
        out.append(_issue("good", "環境", "網路環境正常", "", "閘道、DHCP、DNS 與對外連線都沒有發現異常。"))
    return out


def _is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False
