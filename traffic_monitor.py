"""
traffic_monitor.py — 輕量流量異常監測（NetInspect Pro P2）

用途：短時間監聽介面流量，抓出「誰狂發封包、廣播/群播風暴、ARP 洪水、
      封包迴圈徵兆、可疑大量發送」。**只做統計層級，不解碼封包內容**。

技術：以系統 tcpdump（-e -n -t -l）子程序擷取 L2/L3 標頭，逐行解析後彙整。
      需要 BPF 存取權（root 或 access_bpf 群組 / Wireshark ChmodBPF）。
      無權限時回明確提示，不崩潰。

無額外相依（用標準庫 subprocess + 既有 OUI 查詢）。
"""
import re
import time
import threading
import subprocess

import recommendations
from lan_scanner import _bin, _vendor, _ensure_oui

# tcpdump -e -n -t 一行示例：
#   aa:bb:cc:dd:ee:ff > ff:ff:ff:ff:ff:ff, ethertype ARP (0x0806), length 60: Request ...
#   aa:bb.. > 11:22.., ethertype IPv4 (0x0800), length 98: 192.168.1.5.443 > 8.8.8.8.55751: ...
# 貪婪 .* 讓 VLAN(802.1Q)/QinQ 標籤幀取到最內層（真正 L3）的 ethertype 與 length
_LINE = re.compile(
    r"^([0-9a-f]{2}(?::[0-9a-f]{2}){5}) > ([0-9a-f]{2}(?::[0-9a-f]{2}){5}),"
    r".*ethertype (\S+)[^,]*, length (\d+):")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"        # 0–255，避免抓到內文非 IP 數字
# 不加尾端邊界：tcpdump 以 IP.port 呈現（如 192.168.1.5.443），需能取出前 4 段 IP
_IPV4 = re.compile(r"(?<![\d.])(" + _OCTET + r"(?:\." + _OCTET + r"){3})")
_ARP_TELL = re.compile(r"\btell (" + _OCTET + r"(?:\." + _OCTET + r"){3})")

BROADCAST = "ff:ff:ff:ff:ff:ff"


def _is_multicast(mac: str) -> bool:
    """L2 群播：目的 MAC 第一個 byte 最低位=1（排除廣播）。"""
    try:
        return (int(mac[:2], 16) & 1) == 1 and mac != BROADCAST
    except (ValueError, IndexError):
        return False


def can_capture() -> bool:
    """能否抓封包（BPF 可讀）。"""
    import os
    try:
        for i in range(4):
            p = f"/dev/bpf{i}"
            if os.path.exists(p) and os.access(p, os.R_OK):
                return True
        return os.geteuid() == 0
    except Exception:
        return False


def default_iface() -> str:
    try:
        out = subprocess.run([_bin("route"), "-n", "get", "default"],
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"interface:\s*(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "en0"


def list_interfaces() -> list:
    """回傳可選介面（預設介面優先）。"""
    dflt = default_iface()
    names = []
    try:
        out = subprocess.run([_bin("ifconfig"), "-l"], capture_output=True,
                             text=True, timeout=4).stdout
        # 只列乙太類介面（utun/lo/ppp 無 L2 標頭，tcpdump -e 無法解析）
        names = [n for n in out.split() if n.startswith(("en", "bridge", "eth"))]
    except Exception:
        names = []
    if dflt not in names:                 # 預設路由走 VPN/tunnel 時，改用實體介面
        dflt = names[0] if names else dflt
    if dflt in names:
        names.remove(dflt)
    ordered = [dflt] + names
    return [{"name": n, "is_default": n == dflt} for n in ordered]


def _parse(line: str):
    """回傳 (src_mac, dst_mac, ethertype, length, src_ip|None) 或 None。"""
    m = _LINE.match(line)
    if not m:
        return None
    src, dst, etype, length = m.group(1), m.group(2), m.group(3), int(m.group(4))
    src_ip = None
    rest = line[m.end():]
    if etype == "ARP":
        tm = _ARP_TELL.search(rest)      # ARP 來源 IP 在 "tell X"（who-has 後面是目標，不能用）
        if tm:
            src_ip = tm.group(1)
    elif etype == "IPv4":
        ipm = _IPV4.search(rest)         # 只有 IPv4 幀才記來源 IP，避免非 IP 幀誤判
        if ipm:
            src_ip = ipm.group(1)
    return src, dst, etype, length, src_ip


class Capture:
    """一次流量擷取工作階段。"""

    def __init__(self, iface: str, duration: int):
        self.iface = iface
        self.duration = max(3, min(120, duration))
        self.proc = None
        self._stop = threading.Event()
        self._stderr = []       # tcpdump stderr 尾段（供失敗時回報真正原因）
        # 彙整
        self.by_src = {}        # mac → {"pkts","bytes","bcast","mcast","arp","ip"}
        self.total_pkts = 0
        self.total_bytes = 0
        self.bcast = 0
        self.mcast = 0
        self.started = 0.0

    def stop(self):
        self._stop.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)          # 等 SIGTERM 生效
                except Exception:
                    p.kill()                   # 逾時強制結束，確保不留孤兒程序
            except Exception:
                pass

    def _rec(self, src, dst, etype, length, src_ip):
        s = self.by_src.get(src)
        if s is None:
            s = {"pkts": 0, "bytes": 0, "bcast": 0, "mcast": 0, "arp": 0, "ip": ""}
            self.by_src[src] = s
        s["pkts"] += 1
        s["bytes"] += length
        self.total_pkts += 1
        self.total_bytes += length
        if src_ip and not s["ip"]:
            s["ip"] = src_ip
        if dst == BROADCAST:
            s["bcast"] += 1
            self.bcast += 1
        elif _is_multicast(dst):
            s["mcast"] += 1
            self.mcast += 1
        if etype == "ARP":
            s["arp"] += 1

    def run(self, on_stats=None):
        """啟動 tcpdump 擷取；on_stats(snapshot) 每秒回報一次即時統計。"""
        _ensure_oui()
        if not can_capture():
            raise PermissionError(
                "無法擷取封包：需要系統管理員權限或加入 access_bpf 群組。"
                "可安裝 Wireshark 的 ChmodBPF，或以 sudo 執行本程式。")
        # 注意：不可加 -q，否則輸出格式改變（省略 "ethertype"）導致解析失敗
        cmd = [_bin("tcpdump"), "-i", self.iface, "-n", "-e", "-t", "-l", "-K"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1, creationflags=flags)
        self.started = time.time()
        last_emit = 0.0
        # 持續汲取 stderr（避免 pipe 填滿阻塞，並保留錯誤訊息供失敗時回報）
        def _drain_err():
            try:
                for l in self.proc.stderr:
                    self._stderr.append(l.strip())
                    if len(self._stderr) > 20:
                        self._stderr.pop(0)
            except Exception:
                pass
        errt = threading.Thread(target=_drain_err, daemon=True)
        errt.start()
        watchdog = threading.Thread(target=self._watchdog, daemon=True)
        watchdog.start()
        try:
            for line in self.proc.stdout:
                if self._stop.is_set():
                    break
                p = _parse(line.strip())
                if p:
                    self._rec(*p)
                now = time.time()
                if on_stats and now - last_emit >= 1.0:
                    last_emit = now
                    on_stats(self.snapshot(final=False))
        except Exception:
            pass
        finally:
            self.stop()
        # 完全沒抓到封包且 tcpdump 有錯誤 → 回報真正原因（而非誤導的「封包極少」）
        if self.total_pkts == 0:
            errt.join(timeout=1)
            errtxt = " ".join(self._stderr)
            rc = self.proc.returncode
            if (rc not in (0, None)) or any(k in errtxt for k in (
                    "No such device", "permission", "BIOCSETIF",
                    "not permitted", "syntax error", "failed")):
                raise RuntimeError(f"tcpdump 擷取失敗：{errtxt[:200] or ('return code ' + str(rc))}")
        return self.analyze()

    def _watchdog(self):
        end = self.started + self.duration if self.started else time.time() + self.duration
        while not self._stop.is_set():
            if time.time() >= end:
                self.stop()
                return
            time.sleep(0.2)

    # ── 統計快照 ──
    def _elapsed(self):
        return max(0.5, (time.time() - self.started) if self.started else 0.5)

    def _talkers(self, limit=15):
        el = self._elapsed()
        rows = []
        for mac, s in self.by_src.items():
            share = (s["pkts"] / self.total_pkts * 100) if self.total_pkts else 0
            rows.append({
                "mac": mac, "vendor": _vendor(mac) or "未知", "ip": s["ip"],
                "pkts": s["pkts"], "bytes": s["bytes"],
                "pps": round(s["pkts"] / el, 1),
                "bcast": s["bcast"], "mcast": s["mcast"], "arp": s["arp"],
                "share": round(share, 1),
            })
        rows.sort(key=lambda r: r["pkts"], reverse=True)
        return rows[:limit]

    def snapshot(self, final=False):
        el = self._elapsed()
        return {
            "elapsed": round(el, 1), "duration": self.duration,
            "total_pkts": self.total_pkts, "total_bytes": self.total_bytes,
            "pps": round(self.total_pkts / el, 1),
            "bcast": self.bcast, "mcast": self.mcast,
            "bcast_pct": round(self.bcast / self.total_pkts * 100, 1) if self.total_pkts else 0,
            "sources": len(self.by_src),
            "talkers": self._talkers(),
            "final": final,
        }

    # ── 異常分析（回 recommendations 相同格式，前端共用面板呈現）──
    def analyze(self) -> dict:
        el = self._elapsed()
        snap = self.snapshot(final=True)
        issues = []
        pps = self.total_pkts / el
        bcast_pps = self.bcast / el
        mcast_pps = self.mcast / el
        talkers = self._talkers()

        def iplabel(t):
            return f"{t['vendor']}（{t['ip'] or t['mac']}）"

        if self.total_pkts < 5:
            issues.append({
                "severity": "info", "category": "流量樣本", "icon": "📉",
                "title": "擷取到的封包極少",
                "problem": f"{self.duration} 秒內只擷取到 {self.total_pkts} 個封包。",
                "recommendation": "確認選對介面（有流量的網卡）；交換器環境下本機只看得到自身與廣播流量，"
                                  "需接鏡射埠（SPAN）才能看到全網流量。",
                "value": self.total_pkts,
            })
            res = recommendations._pack(issues, "流量正常，未見異常。")
            res["snapshot"] = snap
            return res

        # 廣播 / 群播風暴 & 迴圈徵兆
        bm_share = (self.bcast + self.mcast) / self.total_pkts * 100
        if bcast_pps > 100 or bm_share > 60:
            issues.append({
                "severity": "critical", "category": "廣播風暴 / 迴圈", "icon": "🌩️",
                "title": f"廣播/群播異常偏高（廣播 {bcast_pps:.0f} pps，占比 {bm_share:.0f}%）",
                "problem": "大量廣播/群播是廣播風暴或封包迴圈（Layer2 loop、STP 未生效）的典型徵兆，會拖垮全網。",
                "recommendation": "檢查交換器是否有環路、確認 STP/RSTP 已啟用；找出廣播來源埠並隔離；"
                                  "留意未受管交換器或重複接線。",
                "value": round(bcast_pps, 1),
            })
        elif bcast_pps > 30:
            issues.append({
                "severity": "warning", "category": "廣播流量", "icon": "📢",
                "title": f"廣播流量偏高（{bcast_pps:.0f} pps）",
                "problem": "廣播封包速率偏高，可能有設定不良或掃描行為。",
                "recommendation": "觀察來源；必要時切分 VLAN 縮小廣播網域。",
                "value": round(bcast_pps, 1),
            })

        # ARP 洪水 / 掃描
        arp_top = sorted(self.by_src.items(), key=lambda kv: kv[1]["arp"], reverse=True)
        if arp_top and arp_top[0][1]["arp"] / el > 20:
            mac, s = arp_top[0]
            issues.append({
                "severity": "warning", "category": "ARP 洪水 / 掃描", "icon": "⚡",
                "title": f"單一來源大量 ARP（{s['arp']/el:.0f} 個/秒）",
                "problem": f"{_vendor(mac) or mac}（{s['ip'] or mac}）短時間發出大量 ARP，"
                           "可能是網段掃描、ARP 欺騙或設備異常。",
                "recommendation": "確認該設備身分與用途；若非預期，隔離並排查是否遭入侵或設定錯誤。",
                "value": round(s["arp"] / el, 1),
            })

        # 可疑大量發送（單一來源佔比過高）
        if talkers and talkers[0]["share"] > 50 and talkers[0]["pps"] > 50:
            t = talkers[0]
            issues.append({
                "severity": "warning", "category": "異常發送來源", "icon": "🚨",
                "title": f"{iplabel(t)} 佔全部流量 {t['share']:.0f}%",
                "problem": f"單一來源送出 {t['pps']:.0f} pps、佔比 {t['share']:.0f}%，明顯高於其他設備。",
                "recommendation": "確認是否為正常大流量應用（備份、串流）；若非預期可能是中毒、迴圈或攻擊來源，應追查。",
                "value": t["share"],
            })

        # 總結（always）
        issues.append({
            "severity": "good", "category": "流量總覽", "icon": "📊",
            "title": f"{self.duration}s 擷取 {self.total_pkts} 封包、{len(self.by_src)} 個來源",
            "problem": "",
            "recommendation": f"✓ 平均 {pps:.0f} pps、廣播占比 {self.bcast/self.total_pkts*100:.0f}%。",
            "value": self.total_pkts,
        })

        result = recommendations._pack(issues, "流量狀況正常，未見明顯異常。")
        result["snapshot"] = self.snapshot(final=True)
        return result


if __name__ == "__main__":
    import json, sys
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    cap = Capture(default_iface(), dur)
    print(f"擷取 {default_iface()} {dur}s…")
    r = cap.run(on_stats=lambda s: print(f"  {s['elapsed']:.0f}s pkts={s['total_pkts']} "
                                         f"pps={s['pps']} bcast%={s['bcast_pct']} 來源={s['sources']}"))
    print(json.dumps({"score": r["score"], "grade": r["grade"]["label"],
                      "issues": [i["title"] for i in r["issues"]],
                      "top": [(t["vendor"], t["ip"], t["pkts"]) for t in r["snapshot"]["talkers"][:5]]},
                     ensure_ascii=False, indent=2))
