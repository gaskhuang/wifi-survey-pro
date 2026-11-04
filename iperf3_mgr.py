"""
iperf3 Manager — server lifecycle + client test runner
Wraps the system iperf3 binary (no Python lib required)
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

# ─── Regex patterns for iperf3 non-JSON output ────────────────────────────────
# 一行區間/摘要：[  5] 或 [SUM]，--bidir 另帶 [TX-C]/[RX-C] 角色標籤；速率單位可無前綴
_RE_LINE = re.compile(
    r'\[\s*(\d+|SUM)\](?:\[(TX|RX)-[CS]\])?\s+([\d.]+)-([\d.]+)\s+sec\s+'
    r'[\d.]+ \S+\s+([\d.]+)\s+([KMG]?)bits/sec(.*)$'
)
_RE_UDP_TAIL = re.compile(r'([\d.]+) ms\s+(\d+)/(\d+)\s+\(([\d.e+-]+)%\)')
_RE_SUMMARY_SENDER   = re.compile(r'sender',   re.IGNORECASE)
_RE_SUMMARY_RECEIVER = re.compile(r'receiver', re.IGNORECASE)

# 參數範圍（避免前端/API 傳入極端值：超長測試、上百條串流）
MIN_DURATION, MAX_DURATION = 1, 120
MIN_STREAMS,  MAX_STREAMS  = 1, 16
DEFAULT_UDP_BW = "100M"          # UDP 未指定頻寬時的預設值（-b 0 會灌爆客戶 LAN）
_RE_BW = re.compile(r'^\d+(?:\.\d+)?[KMGkmg]?$')


def _to_mbps(val: str, unit: str) -> float:
    """Convert rate string to Mbps."""
    v = float(val)
    if unit == "G": return round(v * 1000, 2)
    if unit == "K": return round(v / 1000, 3)
    if unit == "":  return round(v / 1e6, 6)
    return round(v, 2)  # already Mbps


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _udp_bw(bandwidth) -> str:
    """呼叫端有給（且格式合法）就沿用，否則預設 100M。"""
    if bandwidth is None:
        return DEFAULT_UDP_BW
    b = str(bandwidth).strip()
    return b if _RE_BW.match(b) else DEFAULT_UDP_BW

IPERF3_BIN   = "/usr/local/bin/iperf3"   # macOS Homebrew default
DEFAULT_PORT = 5201


def _find_binary() -> str:
    for path in [IPERF3_BIN, "/opt/homebrew/bin/iperf3", "/usr/bin/iperf3"]:
        if os.path.isfile(path):
            return path
    # Try which
    try:
        out = subprocess.check_output(["which", "iperf3"], text=True).strip()
        if out:
            return out
    except Exception:
        pass
    return ""


_FLUSH_CACHE = {}


def _flush_args(binary: str) -> list:
    """iperf3 ≥ 3.1 支援 --forceflush：stdout 接 pipe 時預設是區塊緩衝，
    不加的話「即時」區間行會全部擠到測試結束才一次吐出。"""
    if binary not in _FLUSH_CACHE:
        ok = False
        try:
            out = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r'iperf\s+(\d+)\.(\d+)', out)
            ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 1)
        except Exception:
            ok = False
        _FLUSH_CACHE[binary] = ok
    return ["--forceflush"] if _FLUSH_CACHE[binary] else []


# ─── 進行中的 client 子程序（供 stop_all 收尾）────────────────────────────────
_CLIENTS: set = set()
_CLIENTS_LOCK = threading.Lock()


def _track(proc):
    with _CLIENTS_LOCK:
        _CLIENTS.add(proc)


def _untrack(proc):
    with _CLIENTS_LOCK:
        _CLIENTS.discard(proc)


def _terminate(proc, wait_s: float = 2.0):
    """terminate → wait → kill，確保不留下佔 CPU 的孤兒 iperf3。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=wait_s)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


# ─── Server PID 檔（Cmd+Q 不走 atexit，下次啟動時清掉上次遺留的 iperf3 -s）────────
def _pidfile_path() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/NetInspectPro")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "NetInspectPro")
    else:
        base = os.path.expanduser("~/.config/NetInspectPro")
    return os.path.join(base, "iperf3.pid")


PIDFILE = _pidfile_path()


def _write_pidfile(pid: int):
    try:
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _remove_pidfile():
    try:
        os.remove(PIDFILE)
    except Exception:
        pass


def _pid_is_iperf3(pid: int) -> bool:
    """PID 存活且命令列為 iperf3（避免 PID 被系統重用後誤殺其他程序）。"""
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform.startswith("win"):
        return False                  # 無可靠的命令列查詢，寧可不殺
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmd = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return False
    return "iperf3" in os.path.basename(cmd.split()[0]) if cmd else False


def _kill_stale_server() -> Optional[int]:
    """若 PID 檔指向仍存活的 iperf3，先結束它；回傳被清掉的 PID。"""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        return None
    killed = None
    if _pid_is_iperf3(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        killed = pid
    _remove_pidfile()
    return killed


def check_installed() -> dict:
    binary = _find_binary()
    if not binary:
        return {"installed": False, "binary": "", "version": ""}
    try:
        ver = subprocess.check_output([binary, "--version"],
                                       stderr=subprocess.STDOUT, text=True,
                                       timeout=3).split("\n")[0]
        return {"installed": True, "binary": binary, "version": ver}
    except Exception:
        return {"installed": True, "binary": binary, "version": "unknown"}


def get_local_ips() -> list[str]:
    ips = []
    try:
        # Get all non-loopback IPs
        out = subprocess.check_output(
            ["ifconfig"], text=True, stderr=subprocess.DEVNULL
        )
        import re
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        try:
            ips = [socket.gethostbyname(socket.gethostname())]
        except Exception:
            pass
    return ips


# ─── Server ────────────────────────────────────────────────────────────────────
class IperfServer:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._log:  list[str] = []
        self._port  = DEFAULT_PORT

    def start(self, port: int = DEFAULT_PORT) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"ok": False, "error": "Server already running"}

            binary = _find_binary()
            if not binary:
                return {"ok": False, "error": "iperf3 not found. Run: brew install iperf3"}

            self._port = port
            self._log  = []
            stale = _kill_stale_server()        # 上次 App 未正常結束遺留的 iperf3 -s
            if stale:
                self._log.append(f"[NetInspect] 已結束上次遺留的 iperf3 server（PID {stale}）")
            try:
                self._proc = subprocess.Popen(
                    [binary, "-s", "-p", str(port)] + _flush_args(binary),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                # Give it a moment to bind
                time.sleep(0.4)
                if self._proc.poll() is not None:
                    out = self._proc.stdout.read()
                    return {"ok": False, "error": f"Server exited: {out}"}
                _write_pidfile(self._proc.pid)

                # Background log reader
                threading.Thread(target=self._read_log, daemon=True).start()
                ips = get_local_ips()
                return {
                    "ok":   True,
                    "port": port,
                    "ips":  ips,
                    "pid":  self._proc.pid,
                    "cmd":  f"iperf3 -c {ips[0] if ips else '?'} -p {port}",
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self._proc = None
                _remove_pidfile()
                return {"ok": True, "message": "Server not running"}
            _terminate(self._proc, 3)
            self._proc = None
            _remove_pidfile()
            return {"ok": True}

    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            ips     = get_local_ips() if running else []
            return {
                "running": running,
                "port":    self._port if running else DEFAULT_PORT,
                "ips":     ips,
                "pid":     self._proc.pid if running and self._proc else None,
                "log":     self._log[-20:],
            }

    def _read_log(self):
        if self._proc and self._proc.stdout:
            for line in self._proc.stdout:
                self._log.append(line.rstrip())
                if len(self._log) > 100:
                    self._log = self._log[-100:]


# ─── Client test ──────────────────────────────────────────────────────────────
def _client_cmd(binary, target, port, duration, mode, streams, bandwidth) -> list:
    cmd = [binary, "-c", target, "-p", str(port),
           "-t", str(duration), "-P", str(streams)]
    if mode == "tcp_dl":
        cmd += ["-R"]                              # reverse = download
    elif mode == "tcp_bidir":
        cmd += ["--bidir"]
    elif mode == "udp":
        cmd += ["-u", "-b", _udp_bw(bandwidth)]    # 預設 100M；-b 0（無上限）會灌爆客戶 LAN
    return cmd


def run_test(target: str, port: int = DEFAULT_PORT,
             duration: int = 10, mode: str = "tcp_dl",
             streams: int = 1, bandwidth: Optional[str] = None) -> dict:
    """
    mode: "tcp_dl" | "tcp_ul" | "tcp_bidir" | "udp"
    bandwidth: UDP 目標頻寬（如 "50M"），None → 100M
    Returns structured result with intervals and summary.
    """
    binary = _find_binary()
    if not binary:
        return {"ok": False, "error": "iperf3 not found"}

    duration = _clamp(duration, MIN_DURATION, MAX_DURATION, 10)
    streams  = _clamp(streams, MIN_STREAMS, MAX_STREAMS, 1)
    cmd = _client_cmd(binary, target, port, duration, mode, streams, bandwidth) + ["-J"]

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        _track(proc)
        out, err = proc.communicate(timeout=duration + 15)
        raw = (out or "").strip()
        if not raw:
            return {"ok": False, "error": (err or "").strip() or "No output from iperf3"}

        data = json.loads(raw)
        return _parse_result(data, mode)

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Test timed out"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _terminate(proc)
        _untrack(proc)


def _parse_result(data: dict, mode: str) -> dict:
    end = data.get("end", {})
    intervals = data.get("intervals", [])

    # ── Interval data (per-second throughput for chart) ────────────────────
    series = []
    for iv in intervals:
        s = iv.get("sum") or iv.get("sum_received") or {}
        series.append({
            "t":    round(s.get("start", 0), 1),
            "mbps": round(s.get("bits_per_second", 0) / 1e6, 2),
        })
        # bidir has both sent and received
        if iv.get("sum_received"):
            pass  # already handled

    # ── Summary ────────────────────────────────────────────────────────────
    if mode == "udp":
        s = end.get("sum_received") or end.get("sum", {})   # 接收端統計才有 jitter/遺失
        mbps       = round(s.get("bits_per_second", 0) / 1e6, 2)
        jitter_ms  = round(s.get("jitter_ms", 0), 3)
        lost_pct   = round(s.get("lost_percent", 0), 2)
        lost_pkts  = s.get("lost_packets", 0)
        total_pkts = s.get("packets", 0)
        return {
            "ok": True, "mode": mode,
            "mbps":      mbps,
            "jitter_ms": jitter_ms,
            "lost_pct":  lost_pct,
            "lost_pkts": lost_pkts,
            "total_pkts": total_pkts,
            "series":    series,
            "retransmits": 0,
        }

    # TCP download or upload
    sent = end.get("sum_sent", end.get("sum", {}))
    recv = end.get("sum_received", sent)
    tx_mbps  = round(sent.get("bits_per_second", 0) / 1e6, 2)
    rx_mbps  = round(recv.get("bits_per_second", 0) / 1e6, 2)
    retrans  = sent.get("retransmits", 0)

    # bidir
    rx_series = []
    if mode == "tcp_bidir":
        for iv in intervals:
            r = iv.get("sum_received", {})
            rx_series.append({
                "t":    round(r.get("start", 0), 1),
                "mbps": round(r.get("bits_per_second", 0) / 1e6, 2),
            })

    dl_mbps = rx_mbps if mode in ("tcp_dl", "tcp_bidir") else tx_mbps
    ul_mbps = tx_mbps if mode in ("tcp_ul", "tcp_bidir") else 0

    return {
        "ok":         True,
        "mode":       mode,
        "dl_mbps":    dl_mbps,
        "ul_mbps":    ul_mbps,
        "retransmits": retrans,
        "series":     series,    # sender side
        "rx_series":  rx_series, # bidir receiver
        "jitter_ms":  None,
        "lost_pct":   None,
    }


# ─── Streaming test (non-JSON, line-by-line) ──────────────────────────────────
def _parse_line(line: str, streams: int):
    """解析一行區間/摘要。回傳 dict 或 None。
    多串流（-P>1）只採 [SUM] 列，單串流只採 [ id ] 列，避免同一秒被重複計算。"""
    m = _RE_LINE.search(line)
    if not m:
        return None
    sid, role = m.group(1), m.group(2)
    if (streams > 1) != (sid == "SUM"):
        return None
    tail = m.group(7)
    d = {
        "role":    role,                       # None / "TX" / "RX"（--bidir）
        "t0":      float(m.group(3)),
        "t1":      float(m.group(4)),
        "mbps":    _to_mbps(m.group(5), m.group(6)),
        "summary": "sender" if _RE_SUMMARY_SENDER.search(tail) else
                   ("receiver" if _RE_SUMMARY_RECEIVER.search(tail) else None),
    }
    um = _RE_UDP_TAIL.search(tail)
    if um:
        d.update(jitter_ms=float(um.group(1)), lost=int(um.group(2)),
                 total=int(um.group(3)), lost_pct=float(um.group(4)))
    else:
        nm = re.match(r'\s+(\d+)\b', tail)     # TCP：Retr；UDP 送端區間：Total Datagrams
        d["count"] = int(nm.group(1)) if nm else None
    return d


def stream_test(target: str, port: int, duration: int, mode: str, streams: int,
                bandwidth: Optional[str] = None):
    """
    Generator — yields (event_type, data_dict).
    Events: "start" | "log" | "interval" | "analysis" | "error"
    Runs iperf3 without -J so output is real-time line by line (--forceflush).
    產生器被關閉（SSE 斷線 → GeneratorExit）時於 finally 結束 iperf3 子程序。
    """
    binary = _find_binary()
    if not binary:
        yield "error", {"message": "iperf3 未安裝，請執行: brew install iperf3"}
        return

    duration = _clamp(duration, MIN_DURATION, MAX_DURATION, 10)
    streams  = _clamp(streams, MIN_STREAMS, MAX_STREAMS, 1)
    cmd = (_client_cmd(binary, target, port, duration, mode, streams, bandwidth)
           + ["-i", "1"] + _flush_args(binary))

    yield "start", {
        "cmd":      " ".join(cmd),
        "mode":     mode,
        "duration": duration,
        "target":   target,
        "port":     port,
        "streams":  streams,
        "bandwidth": _udp_bw(bandwidth) if mode == "udp" else None,
    }

    intervals: list[dict] = []
    udp_summary: Optional[dict] = None
    bidir_pending: dict = {}                   # t0 → {"TX": row, "RX": row}
    proc = None

    try:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            _track(proc)

            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                yield "log", {"text": line}

                p = _parse_line(line, streams)
                if not p:
                    continue
                if p["summary"]:
                    # UDP：client 是送端，區間列沒有 jitter/遺失；改取測試結束時的「receiver」摘要
                    if mode == "udp" and p["summary"] == "receiver" and "jitter_ms" in p:
                        udp_summary = p
                    continue
                if p["t1"] - p["t0"] > 1.5:
                    continue

                if mode == "udp":
                    iv = {"t": round(p["t0"], 1), "mbps": p["mbps"],
                          "datagrams": p.get("count") or 0}
                elif mode == "tcp_bidir":
                    # 同一秒的 TX/RX 兩列合併成一筆：mbps = 雙向合計
                    if not p["role"]:
                        continue
                    slot = bidir_pending.setdefault(round(p["t0"], 1), {})
                    slot[p["role"]] = p
                    if len(slot) < 2:
                        continue
                    t = round(p["t0"], 1)
                    bidir_pending.pop(t, None)
                    tx, rx = slot["TX"], slot["RX"]
                    iv = {"t": t, "mbps": round(tx["mbps"] + rx["mbps"], 2),
                          "tx_mbps": tx["mbps"], "rx_mbps": rx["mbps"],
                          "retr": tx.get("count") or 0}
                else:
                    iv = {"t": round(p["t0"], 1), "mbps": p["mbps"],
                          "retr": p.get("count") or 0}
                intervals.append(iv)
                yield "interval", iv

            proc.wait(timeout=5)

        except Exception as e:
            yield "error", {"message": str(e)}
            return

        # ── Post-test analysis ────────────────────────────────────────────────
        analysis = _build_analysis(intervals, mode, duration, udp_summary)
        yield "analysis", analysis
    finally:
        # 不論正常結束、例外或 GeneratorExit（客戶端關閉 SSE）都收掉子程序
        _terminate(proc)
        _untrack(proc)


def _build_analysis(intervals: list, mode: str, duration: int,
                    udp_summary: Optional[dict] = None) -> dict:
    if not intervals:
        return {"ok": False, "error": "未擷取到任何區間資料，請確認 server 端正在運行。"}

    mbps_list = [iv["mbps"] for iv in intervals]
    avg   = round(sum(mbps_list) / len(mbps_list), 1)
    peak  = round(max(mbps_list), 1)
    low   = round(min(mbps_list), 1)

    # Stability: coefficient of variation (%)
    mean_v  = avg if avg > 0 else 1
    std_dev = (sum((x - avg)**2 for x in mbps_list) / len(mbps_list))**0.5
    cv      = round(std_dev / mean_v * 100, 1)

    result: dict = {
        "ok": True, "mode": mode,
        "avg_mbps": avg, "peak_mbps": peak, "low_mbps": low,
        "stability_cv": cv, "intervals": len(intervals),
    }

    if mode == "udp":
        # 方向不變（client→server 上傳）；區間為送端速率，jitter/遺失取 server 回報的接收端摘要
        result["offered_mbps"] = avg
        result["udp_summary"] = bool(udp_summary)
        if udp_summary:
            result["receiver_mbps"] = round(udp_summary["mbps"], 1)
            result["avg_mbps"]      = result["receiver_mbps"]    # 實際送達的速率
            result["avg_jitter_ms"] = round(udp_summary["jitter_ms"], 3)
            result["avg_lost_pct"]  = round(udp_summary["lost_pct"], 2)
            result["total_lost"]    = udp_summary["lost"]
            result["total_pkts"]    = udp_summary["total"]
        else:
            result["avg_jitter_ms"] = None
            result["avg_lost_pct"]  = None
            result["total_lost"]    = None
            result["total_pkts"]    = sum(iv.get("datagrams", 0) for iv in intervals)
            result["note"] = "未取得接收端摘要（server 可能中途中斷），無法計算 jitter/遺失率。"
        avg = result["avg_mbps"]
    else:
        result["total_retransmits"] = sum(iv.get("retr", 0) for iv in intervals)
        if mode == "tcp_bidir":
            tx = [iv.get("tx_mbps", 0) for iv in intervals]
            rx = [iv.get("rx_mbps", 0) for iv in intervals]
            result["avg_tx_mbps"] = round(sum(tx) / len(tx), 1)
            result["avg_rx_mbps"] = round(sum(rx) / len(rx), 1)

    result["speed_tier"]  = _speed_tier(avg)
    result["ratings"]     = _ratings(result, mode)
    result["use_cases"]   = _use_cases(avg)
    result["recommendations"] = _recommendations(result, mode)
    return result


def _speed_tier(mbps: float) -> dict:
    if mbps >= 2000: return {"label":"Wi-Fi 7 (802.11be)","grade":"S","color":"#a855f7"}
    if mbps >= 800:  return {"label":"Wi-Fi 6E (802.11ax 6GHz)","grade":"A+","color":"#22c55e"}
    if mbps >= 400:  return {"label":"Wi-Fi 6 (802.11ax)","grade":"A","color":"#22c55e"}
    if mbps >= 200:  return {"label":"Wi-Fi 5 (802.11ac)","grade":"B","color":"#84cc16"}
    if mbps >= 72:   return {"label":"Wi-Fi 4 (802.11n)","grade":"C","color":"#eab308"}
    if mbps >= 20:   return {"label":"Wi-Fi 4 低速","grade":"D","color":"#f97316"}
    return {"label":"低速/嚴重干擾","grade":"F","color":"#ef4444"}


def _ratings(r: dict, mode: str) -> dict:
    ratings = {}

    # Speed
    mbps = r["avg_mbps"]
    t = _speed_tier(mbps)
    ratings["speed"] = {"label": t["label"], "color": t["color"], "score": _mbps_score(mbps)}

    # Stability
    cv = r["stability_cv"]
    if cv < 5:    ratings["stability"] = {"label":"非常穩定","color":"#22c55e","score":100}
    elif cv < 15: ratings["stability"] = {"label":"穩定","color":"#84cc16","score":75}
    elif cv < 30: ratings["stability"] = {"label":"略有波動","color":"#eab308","score":50}
    else:         ratings["stability"] = {"label":"不穩定","color":"#ef4444","score":25}

    if mode == "udp" and r.get("avg_jitter_ms") is not None:
        jitter = r.get("avg_jitter_ms", 999)
        loss   = r.get("avg_lost_pct", 100)
        if   jitter < 1  and loss < 0.01: ratings["udp"] = {"label":"VoIP 優異","color":"#22c55e","score":100}
        elif jitter < 5  and loss < 0.5:  ratings["udp"] = {"label":"VoIP 可用","color":"#84cc16","score":75}
        elif jitter < 20 and loss < 2:    ratings["udp"] = {"label":"串流可用","color":"#eab308","score":50}
        else:                             ratings["udp"] = {"label":"品質差","color":"#ef4444","score":20}
    elif mode != "udp":
        retr = r.get("total_retransmits", 0)
        if   retr == 0:  ratings["tcp"] = {"label":"無重傳 ✓","color":"#22c55e","score":100}
        elif retr < 10:  ratings["tcp"] = {"label":"輕微重傳","color":"#84cc16","score":75}
        elif retr < 50:  ratings["tcp"] = {"label":"重傳偏多","color":"#eab308","score":50}
        else:            ratings["tcp"] = {"label":"重傳嚴重","color":"#ef4444","score":20}

    return ratings


def _mbps_score(mbps: float) -> int:
    if mbps >= 2000: return 100
    if mbps >= 800:  return 90
    if mbps >= 400:  return 80
    if mbps >= 200:  return 65
    if mbps >= 72:   return 45
    if mbps >= 20:   return 25
    return 10


def _use_cases(mbps: float) -> list[str]:
    cases = []
    if mbps >= 1000: cases.append("8K 串流 ✓")
    if mbps >= 100:  cases.append("4K 串流 ✓")
    if mbps >= 50:   cases.append("1080p 視訊 ✓")
    if mbps >= 25:   cases.append("視訊會議 ✓")
    if mbps >= 10:   cases.append("雲端辦公 ✓")
    if mbps < 10:    cases.append("僅基本瀏覽")
    return cases


def _recommendations(r: dict, mode: str) -> list[dict]:
    recs = []
    mbps = r["avg_mbps"]
    cv   = r["stability_cv"]

    if mbps < 50:
        recs.append({"level":"critical","icon":"🔴",
            "text":f"吞吐量僅 {mbps} Mbps，遠低預期。建議：靠近 AP、切換 5GHz/6GHz、排查同頻干擾。"})
    elif mbps < 200:
        recs.append({"level":"warning","icon":"🟡",
            "text":f"吞吐量 {mbps} Mbps（Wi-Fi 4/5 等級）。切換 5GHz 80MHz 頻道或升級 AP 可提升 3-5 倍。"})
    else:
        tier = _speed_tier(mbps)["label"]
        recs.append({"level":"good","icon":"🟢",
            "text":f"吞吐量 {mbps} Mbps，達 {tier} 標準。"})

    if cv > 20:
        recs.append({"level":"warning","icon":"🟡",
            "text":f"速率波動 ±{cv:.0f}%，可能是同頻干擾或 AP 過載。嘗試換頻道或減少同時連線裝置。"})
    elif cv < 5:
        recs.append({"level":"good","icon":"🟢",
            "text":f"速率非常穩定（波動 ±{cv:.0f}%），連線品質良好。"})

    if mode != "udp":
        retr = r.get("total_retransmits", 0)
        if retr > 50:
            recs.append({"level":"critical","icon":"🔴",
                "text":f"TCP 重傳 {retr} 次，封包遺失嚴重。靠近 AP 或排查 RF 干擾源。"})
        elif retr > 10:
            recs.append({"level":"warning","icon":"🟡",
                "text":f"TCP 重傳 {retr} 次，存在輕微封包遺失，大型檔案傳輸效能受影響。"})
        elif retr == 0:
            recs.append({"level":"good","icon":"🟢","text":"TCP 零重傳，連線穩定可靠。"})

    if mode == "udp" and r.get("avg_jitter_ms") is not None:
        jitter = r.get("avg_jitter_ms", 0)
        loss   = r.get("avg_lost_pct", 0)
        if jitter > 20:
            recs.append({"level":"warning","icon":"🟡",
                "text":f"UDP Jitter {jitter} ms，VoIP/視訊通話可能有頓感。啟用 WMM QoS 可改善。"})
        if loss > 1:
            recs.append({"level":"critical","icon":"🔴",
                "text":f"UDP 封包遺失 {loss}%，即時串流會明顯卡頓。需改善 RF 環境。"})
        if jitter < 5 and loss < 0.1:
            recs.append({"level":"good","icon":"🟢",
                "text":f"UDP Jitter {jitter}ms / 遺失 {loss}%，適合 VoIP 和即時應用。"})

    return recs


# ─── Singleton server instance ─────────────────────────────────────────────────
_server = IperfServer()

def get_server() -> IperfServer:
    return _server


def stop_all() -> dict:
    """結束所有本模組啟動的 iperf3（server + 進行中的 client）並移除 PID 檔。
    供視窗關閉 hook / atexit 呼叫；可重複呼叫。"""
    with _CLIENTS_LOCK:
        procs = list(_CLIENTS)
    for p in procs:
        _terminate(p, 1.5)
        _untrack(p)
    _server.stop()
    return {"ok": True, "clients_stopped": len(procs)}


cleanup = stop_all          # 別名


def cleanup_stale() -> Optional[int]:
    """App 啟動時可呼叫：結束 PID 檔指向的上次遺留 iperf3 -s；回傳被清掉的 PID 或 None。
    （server.start() 本身也會先做這件事）"""
    if _server._proc is not None and _server._proc.poll() is None:
        return None             # 本行程自己的 server 正在跑，不動
    return _kill_stale_server()
