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
import time
import socket
import threading
import ipaddress
import subprocess
from concurrent.futures import ThreadPoolExecutor

import recommendations
from lan_scanner import _bin, _primary_ip, _default_gateway, _icmp_sweep, ping as lan_ping

TEST_IP     = "8.8.8.8"          # 外網連通測試（IP）
TEST_DOMAIN = "www.google.com"   # DNS 解析測試（域名）


# ── 單次診斷內的結果重用 ─────────────────────────────────────────────────────
# diagnose_all 會先並行預取（閘道、本機 IP、ping、DNS…）放進 ctx，各情境直接取用，
# 不再重複 ping 8.8.8.8 / 閘道、重複解析 DNS、重複跑 netstat。單項 diagnose() 不設 ctx。
_tls = threading.local()


def _memo(key, fn, *args):
    ctx = getattr(_tls, "ctx", None)
    if ctx is None:
        return fn(*args)
    if key not in ctx:
        ctx[key] = fn(*args)
    return ctx[key]


def _ip():
    return _memo("ip", _primary_ip)


def _gw():
    return _memo("gw", _default_gateway)


# ── 基礎探測 ─────────────────────────────────────────────────────────────────
def _ping1(host: str, count: int = 1) -> dict:
    """單次連通測試：優先免權限 ICMP socket（不通時約 1.1 s 即判定，ping 子程序需 ~2 s），
    無法建立 socket 才退回系統 ping。呼叫端只用 ["alive"]。"""
    if count == 1:
        alive = _icmp_sweep([host], wait_s=1.0)
        if alive is not None:
            return {"ip": host, "alive": host in alive}
    return lan_ping(host, count)


def _reachable(host: str, count: int = 1) -> dict:
    return _memo(("ping", host, count), _ping1, host, count)


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


def _system_resolve(name: str, timeout: float):
    """系統解析器放在 daemon 執行緒並 join(timeout)：gethostbyname 無逾時，可能卡 ~20 s。
    回傳 (ip|None, error|None)。"""
    box = {}

    def _run():
        try:
            box["ip"] = socket.gethostbyname(name)
        except Exception as e:
            box["err"] = str(e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if "ip" in box:
        return box["ip"], None
    return None, box.get("err") or f"系統解析逾時（>{timeout:.1f}s）"


def dns_resolve(name: str, timeout: float = 3.0) -> dict:
    """解析域名，回傳 {ok, ip, ms}（失敗另含 error、reason）。
    以 dnspython 的 per-resolver timeout 完成，避免改動行程全域 socket 逾時
    （setdefaulttimeout 在多執行緒 Flask 下會互相干擾、非執行緒安全）。
    逾時 / 無可用 DNS / NXDOMAIN / 無 A 記錄 本身就是診斷結果，直接回失敗；
    只有 dnspython 無法使用或讀不到解析設定時，才退回系統解析器（同樣受 timeout 限制）。"""
    t0 = time.time()

    def _ms():
        return round((time.time() - t0) * 1000)

    def _fail(reason, err):
        return {"ok": False, "ip": None, "ms": None, "reason": reason,
                "error": err, "elapsed_ms": _ms()}

    try:
        import dns.resolver
        import dns.exception
    except Exception:
        dns = None
    if dns is not None:
        try:
            r = dns.resolver.Resolver()
            r.timeout = timeout
            r.lifetime = timeout
            ans = r.resolve(name, "A")
            return {"ok": True, "ip": ans[0].to_text(), "ms": _ms()}
        except dns.resolver.NXDOMAIN as e:
            return _fail("nxdomain", f"網域不存在（NXDOMAIN）：{e}")
        except dns.resolver.NoAnswer as e:
            return _fail("no_answer", f"查無 A 記錄：{e}")
        except dns.resolver.NoNameservers as e:
            return _fail("no_nameservers", f"DNS 伺服器皆無法回應/拒絕：{e}")
        except dns.exception.Timeout as e:
            return _fail("timeout", f"DNS 查詢逾時（{timeout:g}s）：{e}")
        except Exception:
            pass                       # 設定類錯誤（如 NoResolverConfiguration）→ 系統解析器
    left = max(0.5, timeout - (time.time() - t0))
    ip, err = _system_resolve(name, left)
    if ip:
        return {"ok": True, "ip": ip, "ms": _ms()}
    return _fail("system", err)


def _dns_test():
    return _memo(("dns", TEST_DOMAIN), dns_resolve, TEST_DOMAIN)


def _servers():
    return _memo("dns_servers", dns_servers)


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


def _route_iface() -> str:
    try:
        out = subprocess.run([_bin("route"), "-n", "get", "default"],
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"interface:\s*(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def route_info() -> dict:
    return {"gateway": _gw(), "interface": _memo("iface", _route_iface), "ip": _ip()}


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
    ip = _ip()
    gw = _gw()

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

    dns = _dns_test()
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
    dns = _dns_test()
    steps.append(_step(f"解析域名（{TEST_DOMAIN}）", dns["ok"],
                       f"→ {dns['ip']}（{dns['ms']}ms）" if dns["ok"] else "解析失敗"))
    servers = _servers()
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
    ip = _ip()
    apipa = ip.startswith("169.254")
    has_ip = bool(ip) and ip != "127.0.0.1" and not apipa
    steps.append(_step("本機 IP", has_ip, ip or "無"))
    steps.append(_step("是否 APIPA（169.254）", not apipa,
                       "是（未取得 DHCP）" if apipa else "否"))
    gw = _gw()
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


def _prefetch() -> dict:
    """並行取得各情境共用的探測結果（總耗時 ≈ 最慢的一項，而非逐項相加）。"""
    ctx = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_gw    = ex.submit(_default_gateway)
        f_ip    = ex.submit(_primary_ip)
        f_inet  = ex.submit(_ping1, TEST_IP, 1)
        f_dns   = ex.submit(dns_resolve, TEST_DOMAIN)
        f_srv   = ex.submit(dns_servers)
        f_iface = ex.submit(_route_iface)
        gw = f_gw.result()
        f_gwp = ex.submit(_ping1, gw, 1) if gw else None
        ctx["gw"] = gw
        ctx["ip"] = f_ip.result()
        ctx[("ping", TEST_IP, 1)] = f_inet.result()
        ctx[("dns", TEST_DOMAIN)] = f_dns.result()
        ctx["dns_servers"] = f_srv.result()
        ctx["iface"] = f_iface.result()
        if f_gwp is not None:
            ctx[("ping", gw, 1)] = f_gwp.result()
    return ctx


def diagnose_all() -> dict:
    """一鍵全項：合併 OSI / DNS / NAT 的問題，給綜合結論。
    探測先並行預取一次，三個情境共用同一份結果（不重複 ping / DNS / 取閘道）。"""
    merged = []
    steps = []
    try:
        ctx = _prefetch()
    except Exception:
        ctx = {}                       # 預取失敗則各情境照常自行探測（仍會在 ctx 內去重）
    prev = getattr(_tls, "ctx", None)
    _tls.ctx = ctx
    try:
        for key in ("osi", "dns", "nat"):
            r = TOPICS[key]()
            steps.append({"topic": key, "steps": r.get("steps", [])})
            merged.extend(i for i in r["issues"] if i["severity"] != "good")
    finally:
        _tls.ctx = prev
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
