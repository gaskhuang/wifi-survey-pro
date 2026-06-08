"""
iperf3 Manager — server lifecycle + client test runner
Wraps the system iperf3 binary (no Python lib required)
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
from typing import Optional

# ─── Regex patterns for iperf3 non-JSON output ────────────────────────────────
# Rate unit can be Kbits/sec, Mbits/sec, Gbits/sec
_RE_RATE = r'([\d.]+)\s+([KMG])bits/sec'

_RE_TCP = re.compile(
    r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+ \S+\s+' + _RE_RATE +
    r'(?:\s+(\d+))?'
)
_RE_UDP = re.compile(
    r'\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+ \S+\s+' + _RE_RATE +
    r'\s+([\d.]+) ms\s+(\d+)/(\d+) \(([\d.]+)%\)'
)
_RE_SUMMARY_SENDER   = re.compile(r'sender',   re.IGNORECASE)
_RE_SUMMARY_RECEIVER = re.compile(r'receiver', re.IGNORECASE)


def _to_mbps(val: str, unit: str) -> float:
    """Convert rate string to Mbps."""
    v = float(val)
    if unit == "G": return round(v * 1000, 2)
    if unit == "K": return round(v / 1000, 3)
    return round(v, 2)  # already Mbps

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
            try:
                self._proc = subprocess.Popen(
                    [binary, "-s", "-p", str(port)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                # Give it a moment to bind
                time.sleep(0.4)
                if self._proc.poll() is not None:
                    out = self._proc.stdout.read()
                    return {"ok": False, "error": f"Server exited: {out}"}

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
                return {"ok": True, "message": "Server not running"}
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
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
def run_test(target: str, port: int = DEFAULT_PORT,
             duration: int = 10, mode: str = "tcp_dl",
             streams: int = 1) -> dict:
    """
    mode: "tcp_dl" | "tcp_ul" | "tcp_bidir" | "udp"
    Returns structured result with intervals and summary.
    """
    binary = _find_binary()
    if not binary:
        return {"ok": False, "error": "iperf3 not found"}

    cmd = [binary, "-c", target, "-p", str(port),
           "-t", str(duration), "-J",              # JSON output
           "-P", str(streams)]                      # parallel streams

    if mode == "tcp_dl":
        cmd += ["-R"]                              # reverse = download
    elif mode == "tcp_bidir":
        cmd += ["--bidir"]
    elif mode == "udp":
        cmd += ["-u", "-b", "0"]                   # UDP unlimited bandwidth

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=duration + 15
        )
        raw = proc.stdout.strip()
        if not raw:
            err = proc.stderr.strip()
            return {"ok": False, "error": err or "No output from iperf3"}

        data = json.loads(raw)
        return _parse_result(data, mode)

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Test timed out"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        s = end.get("sum", {})
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
def stream_test(target: str, port: int, duration: int, mode: str, streams: int):
    """
    Generator — yields (event_type, data_dict).
    Events: "start" | "log" | "interval" | "analysis" | "error"
    Runs iperf3 without -J so output is real-time line by line.
    """
    binary = _find_binary()
    if not binary:
        yield "error", {"message": "iperf3 未安裝，請執行: brew install iperf3"}
        return

    cmd = [binary, "-c", target, "-p", str(port),
           "-t", str(duration), "-P", str(streams), "-i", "1"]
    if mode == "tcp_dl":    cmd += ["-R"]
    elif mode == "tcp_bidir": cmd += ["--bidir"]
    elif mode == "udp":     cmd += ["-u", "-b", "0"]

    yield "start", {
        "cmd":      " ".join(cmd),
        "mode":     mode,
        "duration": duration,
        "target":   target,
        "port":     port,
    }

    intervals: list[dict] = []

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            yield "log", {"text": line}

            # Skip summary lines (contain "sender" / "receiver")
            is_summary = bool(_RE_SUMMARY_SENDER.search(line) or _RE_SUMMARY_RECEIVER.search(line))

            if mode == "udp":
                m = _RE_UDP.search(line)
                if m and not is_summary:
                    t_start, t_end = float(m.group(1)), float(m.group(2))
                    if t_end - t_start <= 1.5:
                        iv = {
                            "t":         round(t_start, 1),
                            "mbps":      _to_mbps(m.group(3), m.group(4)),
                            "jitter_ms": float(m.group(5)),
                            "lost":      int(m.group(6)),
                            "total":     int(m.group(7)),
                            "lost_pct":  float(m.group(8)),
                        }
                        intervals.append(iv)
                        yield "interval", iv
            else:
                m = _RE_TCP.search(line)
                if m and not is_summary:
                    t_start, t_end = float(m.group(1)), float(m.group(2))
                    if t_end - t_start <= 1.5:
                        iv = {
                            "t":    round(t_start, 1),
                            "mbps": _to_mbps(m.group(3), m.group(4)),
                            "retr": int(m.group(5)) if m.group(5) else 0,
                        }
                        intervals.append(iv)
                        yield "interval", iv

        proc.wait()

    except Exception as e:
        yield "error", {"message": str(e)}
        return

    # ── Post-test analysis ────────────────────────────────────────────────────
    analysis = _build_analysis(intervals, mode, duration)
    yield "analysis", analysis


def _build_analysis(intervals: list, mode: str, duration: int) -> dict:
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
        j_list = [iv["jitter_ms"] for iv in intervals]
        l_list = [iv["lost_pct"]  for iv in intervals]
        result["avg_jitter_ms"] = round(sum(j_list)/len(j_list), 3) if j_list else 0
        result["avg_lost_pct"]  = round(sum(l_list)/len(l_list), 2) if l_list else 0
        result["total_lost"]    = sum(iv["lost"] for iv in intervals)
        result["total_pkts"]    = sum(iv["total"] for iv in intervals)
    else:
        result["total_retransmits"] = sum(iv.get("retr", 0) for iv in intervals)

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

    if mode == "udp":
        jitter = r.get("avg_jitter_ms", 999)
        loss   = r.get("avg_lost_pct", 100)
        if   jitter < 1  and loss < 0.01: ratings["udp"] = {"label":"VoIP 優異","color":"#22c55e","score":100}
        elif jitter < 5  and loss < 0.5:  ratings["udp"] = {"label":"VoIP 可用","color":"#84cc16","score":75}
        elif jitter < 20 and loss < 2:    ratings["udp"] = {"label":"串流可用","color":"#eab308","score":50}
        else:                             ratings["udp"] = {"label":"品質差","color":"#ef4444","score":20}
    else:
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

    if mode == "udp":
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
