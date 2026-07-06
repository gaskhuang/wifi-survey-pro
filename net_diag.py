"""
net_diag.py — 網路診斷工具 + AI 分層排查（NetInspect Pro P3）

把工程師現場常用的判斷做成一鍵按鈕，離線規則引擎即可判讀（無需網路/金鑰）：
  · OSI 由下而上排查：實體/連線 → IP → 閘道 → DNS → 外網
  · DNS 失敗 vs 真斷線：通 IP 不通域名 = DNS；都不通 = 線路
  · DHCP / IP 衝突：是否拿到 IP、APIPA(169.254)、IP 衝突
  · NAT / 路由：預設路由、閘道、對外連通
  · 子網計算機：CIDR → 網段/遮罩/可用範圍/主機數（現場心算替代）
輸出與 recommendations 相同格式（score/grade/issues/counts），前端共用建議面板。

進階：若設定了 AI 供應商（BYOK/雲端），可附「AI 深入分析」文字（best-effort）。
"""
import re
import socket
import ipaddress
import subprocess

import recommendations
from lan_scanner import _bin, _primary_ip, _default_gateway, ping as lan_ping

TEST_IP     = "8.8.8.8"          # 外網連通測試（IP）
TEST_DOMAIN = "www.google.com"   # DNS 解析測試（域名）


# ── 基礎探測 ─────────────────────────────────────────────────────────────────
def _reachable(host: str, count: int = 1) -> dict:
    return lan_ping(host, count)


def dns_servers() -> list:
    """取得系統 DNS 伺服器（macOS: scutil --dns）。"""
    servers = []
    try:
        out = subprocess.run([_bin("scutil"), "--dns"], capture_output=True,
                             text=True, timeout=4).stdout
        for m in re.finditer(r"nameserver\[\d+\]\s*:\s*([\d.]+)", out):
            if m.group(1) not in servers:
                servers.append(m.group(1))
    except Exception:
        pass
    return servers


def dns_resolve(name: str, timeout: float = 3.0) -> dict:
    """解析域名，回傳 {ok, ip, ms}。
    以 dnspython 的 per-resolver timeout 完成，避免改動行程全域 socket 逾時
    （setdefaulttimeout 在多執行緒 Flask 下會互相干擾、非執行緒安全）。"""
    import time
    t0 = time.time()
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.timeout = timeout
        r.lifetime = timeout
        ans = r.resolve(name, "A")
        ip = ans[0].to_text()
        return {"ok": True, "ip": ip, "ms": round((time.time() - t0) * 1000)}
    except Exception:
        # 後援：系統解析器（不觸碰全域 socket 逾時）
        try:
            ip = socket.gethostbyname(name)
            return {"ok": True, "ip": ip, "ms": round((time.time() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "ip": None, "ms": None, "error": str(e)}


def traceroute(host: str, max_hops: int = 15) -> dict:
    """traceroute（-n 不反解、-q 1 每跳一次、-w 1 逾時 1s）。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            [_bin("traceroute"), "-n", "-q", "1", "-w", "1", "-m", str(max_hops), host],
            capture_output=True, text=True, timeout=max_hops * 2 + 10,
            creationflags=flags).stdout
        hops = []
        for line in out.splitlines()[1:]:
            m = re.match(r"\s*(\d+)\s+(.+)", line)
            if not m:
                continue
            ipm = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", m.group(2))
            msm = re.search(r"([\d.]+)\s*ms", m.group(2))
            hops.append({"hop": int(m.group(1)),
                         "ip": ipm.group(1) if ipm else "*",
                         "ms": float(msm.group(1)) if msm else None})
        return {"ok": True, "hops": hops}
    except Exception as e:
        return {"ok": False, "hops": [], "error": str(e)}


def route_info() -> dict:
    gw = _default_gateway()
    iface = ""
    try:
        out = subprocess.run([_bin("route"), "-n", "get", "default"],
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"interface:\s*(\S+)", out)
        if m:
            iface = m.group(1)
    except Exception:
        pass
    return {"gateway": gw, "interface": iface, "ip": _primary_ip()}


# ── 子網計算機 ───────────────────────────────────────────────────────────────
def subnet_calc(cidr: str) -> dict:
    """CIDR / IP+遮罩 → 完整子網資訊。"""
    net = ipaddress.ip_network(cidr, strict=False)
    hosts_total = net.num_addresses - 2 if net.version == 4 and net.num_addresses > 2 \
        else net.num_addresses
    first = last = "—"
    if net.version == 4 and net.num_addresses > 2:
        first = str(net.network_address + 1)
        last = str(net.broadcast_address - 1)
    return {
        "cidr": str(net),
        "network": str(net.network_address),
        "netmask": str(net.netmask),
        "wildcard": str(net.hostmask),
        "broadcast": str(net.broadcast_address) if net.version == 4 else "—",
        "prefix": net.prefixlen,
        "first_host": first,
        "last_host": last,
        "num_hosts": hosts_total,
        "is_private": net.is_private,
        "version": net.version,
    }


# ── 診斷情境（離線規則引擎）──────────────────────────────────────────────────
def _step(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def diag_osi() -> dict:
    """OSI 由下而上：連線 → IP → 閘道 → DNS → 外網，指出斷在哪一層。"""
    steps = []
    issues = []
    ip = _primary_ip()
    gw = _default_gateway()

    has_ip = bool(ip) and ip != "127.0.0.1" and not ip.startswith("169.254")
    apipa = ip.startswith("169.254")
    steps.append(_step("第 1–2 層 · 連線 / 網卡", has_ip or apipa,
                       f"本機 IP：{ip or '無'}"))
    steps.append(_step("第 3 層 · 取得 IP 位址", has_ip,
                       "APIPA（169.254）自動私有位址，代表沒拿到 DHCP" if apipa
                       else (f"IP：{ip}" if has_ip else "未取得有效 IP")))

    gw_ok = False
    if gw:
        gw_ok = _reachable(gw)["alive"]
    steps.append(_step("第 3 層 · 連到預設閘道", gw_ok,
                       f"閘道 {gw or '未知'}：{'可達' if gw_ok else '不可達'}"))

    inet_ok = _reachable(TEST_IP)["alive"]
    steps.append(_step("對外連通（Ping 8.8.8.8）", inet_ok,
                       "可連到外部 IP" if inet_ok else "無法連到外部 IP"))

    dns = dns_resolve(TEST_DOMAIN)
    steps.append(_step("第 7 層 · DNS 解析", dns["ok"],
                       f"{TEST_DOMAIN} → {dns['ip']}（{dns['ms']}ms）" if dns["ok"]
                       else "域名無法解析"))

    # 依「最低失敗層」給結論
    if not has_ip:
        if apipa:
            issues.append(_issue("critical", "DHCP", "🚫", "沒有從 DHCP 取得 IP（APIPA）",
                                 f"本機為 169.254 自動位址，代表連不到 DHCP 伺服器。",
                                 "檢查網路線/AP 連線、DHCP 伺服器是否正常、VLAN 是否正確；手動設固定 IP 可暫時排除。"))
        else:
            issues.append(_issue("critical", "連線", "🔌", "沒有有效 IP 位址",
                                 "網卡未取得可用 IP。", "檢查實體連線（Link 燈）、Wi-Fi 是否連上、網卡是否啟用。"))
    elif not gw_ok:
        issues.append(_issue("critical", "閘道", "🚪", "連不到預設閘道",
                             f"有 IP（{ip}）但 ping 不到閘道 {gw}。",
                             "同網段但不通：檢查 VLAN 標籤/Native VLAN、交換器埠、閘道設備；確認子網遮罩正確。"))
    elif not inet_ok and not dns["ok"]:
        issues.append(_issue("critical", "對外線路", "🌐", "閘道通但對外不通",
                             "可連閘道，但外網 IP 與 DNS 都不通。",
                             "問題在閘道對外：檢查 ISP 線路/數據機、NAT/防火牆規則、對外頻寬。"))
    elif inet_ok and not dns["ok"]:
        issues.append(_issue("warning", "DNS", "🔎", "可連外網 IP，但 DNS 解析失敗",
                             "Ping 得到 8.8.8.8 卻無法解析域名 → 純 DNS 問題。",
                             "更換 DNS 為 1.1.1.1 / 8.8.8.8；檢查 DHCP 派發的 DNS 是否正確、內部 DNS 伺服器是否正常。"))
    else:
        issues.append(_issue("good", "整體連通", "✅", "各層皆正常",
                             "", "✓ 連線 → IP → 閘道 → 外網 → DNS 全數通過。"))

    r = recommendations._pack(issues, "網路各層連通正常。")
    r["steps"] = steps
    return r


def diag_dns() -> dict:
    """DNS 失敗 vs 真斷線。"""
    steps, issues = [], []
    ip_ok = _reachable(TEST_IP)["alive"]
    steps.append(_step("Ping 外部 IP（8.8.8.8）", ip_ok, "可達" if ip_ok else "不可達"))
    dns = dns_resolve(TEST_DOMAIN)
    steps.append(_step(f"解析域名（{TEST_DOMAIN}）", dns["ok"],
                       f"→ {dns['ip']}（{dns['ms']}ms）" if dns["ok"] else "解析失敗"))
    servers = dns_servers()
    steps.append(_step("目前 DNS 伺服器", bool(servers), "、".join(servers) or "未取得"))

    if ip_ok and not dns["ok"]:
        issues.append(_issue("critical", "DNS", "🔎", "純 DNS 問題（線路正常）",
                             f"可連到 8.8.8.8，但無法解析域名。目前 DNS：{'、'.join(servers) or '未知'}。",
                             "改用 1.1.1.1 或 8.8.8.8；檢查內部 DNS 伺服器、DHCP 派發的 DNS、防火牆是否擋 53 埠。"))
    elif not ip_ok and not dns["ok"]:
        issues.append(_issue("critical", "線路", "🔌", "真斷線（非 DNS 問題）",
                             "外部 IP 與域名都不通，問題在連線/線路而非 DNS。",
                             "回到 OSI 由下而上排查：先確認 IP、閘道、對外線路。"))
    else:
        issues.append(_issue("good", "DNS", "✅", "DNS 解析正常",
                             "", f"✓ 域名可解析（{dns['ms']}ms）、外網可達。"))
    r = recommendations._pack(issues, "DNS 與連線正常。")
    r["steps"] = steps
    return r


def diag_dhcp() -> dict:
    """DHCP 取址 / IP 衝突。"""
    steps, issues = [], []
    ip = _primary_ip()
    apipa = ip.startswith("169.254")
    has_ip = bool(ip) and ip != "127.0.0.1" and not apipa
    steps.append(_step("本機 IP", has_ip, ip or "無"))
    steps.append(_step("是否 APIPA（169.254）", not apipa,
                       "是（未取得 DHCP）" if apipa else "否"))
    gw = _default_gateway()
    gw_ok = _reachable(gw)["alive"] if gw else False
    steps.append(_step("閘道可達", gw_ok, f"{gw or '未知'}"))

    if apipa:
        issues.append(_issue("critical", "DHCP", "🚫", "未取得 DHCP 位址（APIPA）",
                             "本機為 169.254 自動位址。",
                             "檢查 DHCP 伺服器是否運作、位址池是否耗盡、VLAN/中繼(relay)設定；重新續租(續約) IP。"))
    elif not has_ip:
        issues.append(_issue("critical", "IP", "🔌", "沒有有效 IP",
                             "", "檢查實體連線與網卡狀態。"))
    else:
        issues.append(_issue("good", "DHCP", "✅", f"已取得 IP（{ip}）",
                             "", "✓ DHCP 取址正常。"))
    issues.append(_issue("info", "IP 衝突", "⚠️", "IP 衝突偵測建議",
                         "本機工具無法完整偵測整網 IP 衝突。",
                         "如懷疑衝突：用「有線 LAN 盤查」查是否有同 IP 多 MAC；或於閘道/交換器查 ARP 表。"))
    r = recommendations._pack(issues, "DHCP 取址正常。")
    r["steps"] = steps
    return r


def diag_nat_route() -> dict:
    """NAT / 路由 / 對外。"""
    steps, issues = [], []
    ri = route_info()
    steps.append(_step("預設路由", bool(ri["gateway"]),
                       f"閘道 {ri['gateway'] or '無'} via {ri['interface'] or '?'}"))
    gw_ok = _reachable(ri["gateway"])["alive"] if ri["gateway"] else False
    steps.append(_step("閘道可達", gw_ok, "可達" if gw_ok else "不可達"))
    inet_ok = _reachable(TEST_IP)["alive"]
    steps.append(_step("對外連通", inet_ok, "可連外網" if inet_ok else "不可連外網"))

    if not ri["gateway"]:
        issues.append(_issue("critical", "路由", "🧭", "沒有預設路由(閘道)",
                             "缺少預設閘道，封包無法送出本網段。",
                             "設定正確的預設閘道；DHCP 環境確認有派發 Router 選項。"))
    elif gw_ok and not inet_ok:
        issues.append(_issue("critical", "NAT/對外", "🌐", "閘道通但對外不通",
                             "可連閘道但外網不通，通常是 NAT/防火牆或 ISP 線路。",
                             "檢查閘道 NAT 是否正常、防火牆對外規則、數據機/ISP 狀態。"))
    else:
        issues.append(_issue("good", "路由/NAT", "✅", "路由與對外連通正常",
                             "", f"✓ 經 {ri['interface']} 由閘道 {ri['gateway']} 對外正常。"))
    r = recommendations._pack(issues, "路由與 NAT 正常。")
    r["steps"] = steps
    return r


def _issue(sev, cat, icon, title, problem, rec):
    return {"severity": sev, "category": cat, "icon": icon, "title": title,
            "problem": problem, "recommendation": rec, "value": ""}


# ── 分派 ─────────────────────────────────────────────────────────────────────
TOPICS = {
    "osi": diag_osi, "dns": diag_dns, "dhcp": diag_dhcp, "nat": diag_nat_route,
}


def diagnose(topic: str) -> dict:
    fn = TOPICS.get(topic)
    if not fn:
        return recommendations._pack([_issue("info", "未知", "❓",
                                     f"未知診斷項目：{topic}", "", "請選擇支援的項目。")])
    return fn()


def diagnose_all() -> dict:
    """一鍵全項：合併 OSI / DNS / NAT 的問題，給綜合結論。"""
    merged = []
    steps = []
    for key in ("osi", "dns", "nat"):
        r = TOPICS[key]()
        steps.append({"topic": key, "steps": r.get("steps", [])})
        merged.extend(i for i in r["issues"] if i["severity"] != "good")
    if not merged:
        merged.append(_issue("good", "整體", "✅", "全項診斷通過",
                             "", "✓ 連線、DNS、路由/NAT 皆正常。"))
    r = recommendations._pack(merged, "全項診斷通過。")
    r["groups"] = steps
    return r


if __name__ == "__main__":
    import json
    print("subnet 192.168.1.10/24:",
          json.dumps(subnet_calc("192.168.1.10/24"), ensure_ascii=False))
    for t in ("osi", "dns", "dhcp", "nat"):
        r = diagnose(t)
        print(f"\n[{t}] score={r['score']} {r['grade']['label']}")
        for s in r["steps"]:
            print(f"   {'✓' if s['ok'] else '✗'} {s['name']}: {s['detail']}")
        print("   →", r["issues"][0]["title"])
