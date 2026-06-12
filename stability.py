"""
stability.py — 一鍵 Wi-Fi 連線穩定度分析
固定時間窗內持續取樣 RSSI/SNR/Tx rate，同步對「閘道器」與「外部主機」做
連續 ping（延遲/抖動/掉包），偵測漫遊/斷線事件，最後評分並產出建議。

閘道器 ping 與外部 ping 分開量測，可區分「無線鏈路不穩」與「對外線路不穩」。
"""
import platform
import re
import statistics
import subprocess
import threading
import time

import scanner

_PLATFORM = platform.system()
_TIME_RE  = re.compile(r"time[=<]([\d.]+)\s*ms")
_IP_RE    = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


# ─── Gateway discovery ──────────────────────────────────────────────────────
def default_gateway() -> str | None:
    try:
        if _PLATFORM == "Windows":
            out = subprocess.check_output(
                ["route", "print", "0.0.0.0"], timeout=5,
                stderr=subprocess.DEVNULL, text=True, errors="replace")
            for line in out.splitlines():
                parts = line.split()
                # 語言無關：0.0.0.0  0.0.0.0  <gateway>  <iface ip>  metric
                if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    if _IP_RE.fullmatch(parts[2]):
                        return parts[2]
        else:
            out = subprocess.check_output(
                ["route", "-n", "get", "default"], timeout=5,
                stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                if "gateway:" in line:
                    m = _IP_RE.search(line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return None


# ─── Continuous ping worker ─────────────────────────────────────────────────
class _PingWorker(threading.Thread):
    """每秒 ping 一次指定主機，逐行解析 RTT；timeout 視為掉包。"""

    def __init__(self, host: str, duration: int):
        super().__init__(daemon=True)
        self.host     = host
        self.duration = duration
        self.samples: list[dict] = []   # {"t": sec, "ms": float|None}
        self._lock    = threading.Lock()
        self._proc    = None

    def run(self):
        count = max(self.duration, 1)
        if _PLATFORM == "Windows":
            cmd = ["ping", "-n", str(count), self.host]
        else:
            cmd = ["ping", "-i", "1", "-c", str(count), self.host]
        t0 = time.time()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors="replace")
            for line in self._proc.stdout:
                el = round(time.time() - t0, 1)
                m  = _TIME_RE.search(line)
                if m:
                    self._add({"t": el, "ms": float(m.group(1))})
                elif "timeout" in line.lower() or "timed out" in line.lower() \
                        or "unreachable" in line.lower():
                    self._add({"t": el, "ms": None})
            self._proc.wait(timeout=10)
        except Exception:
            pass

    def _add(self, s):
        with self._lock:
            self.samples.append(s)

    def terminate(self):
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)

    def stats(self) -> dict:
        pts  = self.snapshot()
        rtts = [p["ms"] for p in pts if p["ms"] is not None]
        lost = sum(1 for p in pts if p["ms"] is None)
        total = len(pts)
        jitter = None
        if len(rtts) >= 2:
            diffs  = [abs(rtts[i] - rtts[i-1]) for i in range(1, len(rtts))]
            jitter = round(sum(diffs) / len(diffs), 1)
        return {
            "host":     self.host,
            "avg_ms":   round(statistics.mean(rtts), 1) if rtts else None,
            "min_ms":   round(min(rtts), 1) if rtts else None,
            "max_ms":   round(max(rtts), 1) if rtts else None,
            "jitter_ms": jitter,
            "loss_pct": round(lost / total * 100, 1) if total else None,
            "sent":     total,
            "lost":     lost,
        }


# ─── Fast connection sampling ───────────────────────────────────────────────
def _fast_sample() -> dict:
    """輕量取樣：macOS 直接走 CoreWLAN（毫秒級），避開 current_connection()
    內的 system_profiler（每次 ~3 秒，會把取樣率拖到 1/4）。"""
    if _PLATFORM != "Darwin":
        return scanner.current_connection()
    try:
        iface = scanner._get_iface()
        if iface is None:
            return {"connected": False}
        rssi_raw  = iface.rssiValue()
        noise_raw = iface.noiseMeasurement()
        rssi  = int(rssi_raw)  if rssi_raw  is not None else -100
        noise = int(noise_raw) if noise_raw is not None else -100
        if rssi == -100 and noise == -100:
            return {"connected": False}
        ch = iface.wlanChannel()
        tx = iface.transmitRate()
        return {
            "connected": True,
            "ssid":    iface.ssid() or "",
            "bssid":   iface.bssid() or "",
            "rssi":    rssi,
            "noise":   noise,
            "snr":     rssi - noise,
            "tx_rate": round(float(tx), 1) if tx else 0.0,
            "channel": int(ch.channelNumber()) if ch else 0,
        }
    except Exception:
        return scanner.current_connection()


# ─── Numeric helpers ────────────────────────────────────────────────────────
def _series_stats(vals: list[float]) -> dict:
    if not vals:
        return {"mean": None, "min": None, "max": None, "std": None}
    return {
        "mean": round(statistics.mean(vals), 1),
        "min":  round(min(vals), 1),
        "max":  round(max(vals), 1),
        "std":  round(statistics.pstdev(vals), 2) if len(vals) > 1 else 0.0,
    }


def _grade(score: int) -> dict:
    if score >= 85: return {"label": "非常穩定",       "emoji": "🟢", "color": "#22c55e"}
    if score >= 70: return {"label": "穩定",           "emoji": "🟢", "color": "#84cc16"}
    if score >= 50: return {"label": "尚可（偶有波動）", "emoji": "🟡", "color": "#eab308"}
    if score >= 30: return {"label": "不穩定",         "emoji": "🟠", "color": "#f97316"}
    return {"label": "非常不穩定", "emoji": "🔴", "color": "#ef4444"}


# ─── Main test runner ───────────────────────────────────────────────────────
class StabilityTest:
    SAMPLE_INTERVAL = 1.0

    def __init__(self):
        self._lock     = threading.Lock()
        self._running  = False
        self._thread   = None
        self._samples: list[dict] = []
        self._events:  list[dict] = []
        self._gw_worker:   _PingWorker | None = None
        self._inet_worker: _PingWorker | None = None
        self._t0       = 0.0
        self._duration = 60
        self.result_data: dict | None = None

    # ── Control ──────────────────────────────────────────────────────────
    def start(self, duration: int = 60, inet_host: str = "8.8.8.8") -> bool:
        with self._lock:
            if self._running:
                return False
            self._running     = True
            self._duration    = max(10, min(600, duration))
            self._samples     = []
            self._events      = []
            self.result_data  = None
        self._thread = threading.Thread(
            target=self._loop, args=(inet_host,), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False

    # ── Status (polled by frontend during test) ─────────────────────────
    def status(self) -> dict:
        with self._lock:
            samples = list(self._samples)
            events  = list(self._events)
        gw   = self._gw_worker.snapshot()   if self._gw_worker   else []
        inet = self._inet_worker.snapshot() if self._inet_worker else []
        elapsed = round(time.time() - self._t0, 1) if self._running else 0
        return {
            "running":  self._running,
            "elapsed":  min(elapsed, self._duration),
            "duration": self._duration,
            "samples":  samples,
            "ping_gw":  gw,
            "ping_inet": inet,
            "events":   events,
            "has_result": self.result_data is not None,
        }

    # ── Sampling loop ─────────────────────────────────────────────────────
    def _loop(self, inet_host: str):
        # 完整連線資訊只抓一次當報告 metadata（內含 system_profiler，較慢）
        self._meta = scanner.current_connection()
        self._t0 = time.time()
        gw = default_gateway()

        self._gw_worker   = _PingWorker(gw, self._duration) if gw else None
        self._inet_worker = _PingWorker(inet_host, self._duration)
        if self._gw_worker:
            self._gw_worker.start()
        self._inet_worker.start()

        last_bssid   = None
        last_channel = None
        was_conn     = None

        while self._running:
            el = time.time() - self._t0
            if el >= self._duration:
                break
            cur  = _fast_sample()
            conn = bool(cur.get("connected"))
            sample = {
                "t":         round(el, 1),
                "connected": conn,
                "rssi":      cur.get("rssi"),
                "noise":     cur.get("noise"),
                "snr":       cur.get("snr"),
                "tx_rate":   cur.get("tx_rate"),
                "channel":   cur.get("channel"),
                "bssid":     cur.get("bssid"),
                "ssid":      cur.get("ssid"),
            }

            # ── Event detection ──
            if was_conn is True and not conn:
                self._push_event(el, "disconnect", "Wi-Fi 連線中斷")
            elif was_conn is False and conn:
                self._push_event(el, "reconnect", "Wi-Fi 重新連線")
            if conn:
                bssid = cur.get("bssid") or ""
                chan  = cur.get("channel")
                if last_bssid and bssid and bssid != last_bssid:
                    self._push_event(el, "roam",
                        f"漫遊：{last_bssid} → {bssid}")
                elif last_bssid == bssid and last_channel and chan \
                        and chan != last_channel:
                    self._push_event(el, "channel_change",
                        f"頻道切換：ch{last_channel} → ch{chan}")
                if bssid:
                    last_bssid = bssid
                if chan:
                    last_channel = chan
            was_conn = conn

            with self._lock:
                self._samples.append(sample)

            # 對齊取樣間隔（current_connection 本身耗時不定）
            next_t = self._t0 + (len(self._samples)) * self.SAMPLE_INTERVAL
            time.sleep(max(0.05, next_t - time.time()))

        # 等 ping 收尾
        if self._gw_worker:
            self._gw_worker.join(timeout=8)
            self._gw_worker.terminate()
        self._inet_worker.join(timeout=8)
        self._inet_worker.terminate()

        self.result_data = self._build_result()
        self._running = False

    def _push_event(self, el: float, etype: str, detail: str):
        with self._lock:
            self._events.append({"t": round(el, 1), "type": etype, "detail": detail})

    # ── Result / scoring ─────────────────────────────────────────────────
    def _build_result(self) -> dict:
        with self._lock:
            samples = list(self._samples)
            events  = list(self._events)

        conn_samples = [s for s in samples if s["connected"]]
        rssi = _series_stats([s["rssi"] for s in conn_samples if s["rssi"] is not None])
        snr  = _series_stats([s["snr"]  for s in conn_samples if s["snr"]  is not None])
        tx   = _series_stats([float(s["tx_rate"]) for s in conn_samples
                              if s["tx_rate"]])

        gw_stats   = self._gw_worker.stats()   if self._gw_worker else None
        inet_stats = self._inet_worker.stats() if self._inet_worker else None
        gw_pings   = self._gw_worker.snapshot()   if self._gw_worker else []
        inet_pings = self._inet_worker.snapshot() if self._inet_worker else []

        n_roam = sum(1 for e in events if e["type"] == "roam")
        n_disc = sum(1 for e in events if e["type"] == "disconnect")

        findings: list[dict] = []
        score = 100

        def add(sev, icon, title, detail, rec, penalty=0):
            nonlocal score
            findings.append({"severity": sev, "icon": icon, "title": title,
                             "detail": detail, "recommendation": rec})
            score -= penalty

        # ── 連線存在性 ──
        if not conn_samples:
            add("critical", "📵", "測試期間完全沒有 Wi-Fi 連線",
                "無法取得任何連線樣本",
                "請先連上 Wi-Fi 再執行穩定度測試", 100)
            return self._finalize(samples, events, rssi, snr, tx,
                                  gw_stats, inet_stats, gw_pings, inet_pings,
                                  findings, max(0, score))

        # ── 斷線 ──
        if n_disc:
            add("critical", "🔌", f"測試期間斷線 {n_disc} 次",
                "連線中斷會直接造成視訊/遊戲/傳輸失敗",
                "檢查 AP 供電與韌體；若裝置省電模式造成斷線，關閉 Wi-Fi 節能選項",
                min(50, 25 * n_disc))

        # ── 漫遊 ──
        if n_roam:
            add("warning", "🔀", f"測試期間漫遊 {n_roam} 次",
                "頻繁在 AP 之間切換會造成瞬斷與延遲尖峰",
                "調整 AP 發射功率或漫遊靈敏度（roaming aggressiveness），"
                "確保固定位置使用時黏在訊號最佳的 AP",
                min(20, 10 * n_roam))

        # ── RSSI 波動 ──
        if rssi["std"] is not None:
            if rssi["std"] > 6:
                add("warning", "📶", f"訊號強度波動大（標準差 {rssi['std']} dB）",
                    f"RSSI 在 {rssi['min']} ～ {rssi['max']} dBm 之間跳動",
                    "檢查路徑上是否有人員走動/門開關/微波爐等間歇性干擾源，"
                    "或將 AP 移至視線可及位置", 15)
            elif rssi["std"] > 4:
                add("warning", "📶", f"訊號強度有波動（標準差 {rssi['std']} dB）",
                    f"RSSI 範圍 {rssi['min']} ～ {rssi['max']} dBm",
                    "觀察波動是否集中在特定時段，排除環境干擾源", 8)
            elif rssi["std"] > 2.5:
                add("info", "📶", f"訊號強度輕微波動（標準差 {rssi['std']} dB)",
                    "屬正常範圍邊緣", "無需特別處理，持續觀察即可", 3)
            else:
                add("good", "📶", "訊號強度平穩",
                    f"RSSI 標準差僅 {rssi['std']} dB", "保持現狀")

        # ── RSSI 絕對值 ──
        if rssi["mean"] is not None:
            if rssi["mean"] < -75:
                add("critical", "🔻", f"訊號過弱（平均 {rssi['mean']} dBm）",
                    "低於 -75 dBm 時連線品質難以穩定",
                    "縮短與 AP 的距離、減少隔牆，或增設 AP / Mesh 節點", 15)
            elif rssi["mean"] < -70:
                add("warning", "🔻", f"訊號偏弱（平均 {rssi['mean']} dBm）",
                    "邊緣覆蓋區，易受環境變化影響",
                    "建議調整 AP 位置或新增節點改善覆蓋", 8)
            elif rssi["mean"] < -65:
                add("info", "📡", f"訊號中等（平均 {rssi['mean']} dBm）",
                    "一般使用尚可，高吞吐應用可能受限",
                    "如需更高速率可優化 AP 擺放位置", 3)
            else:
                add("good", "📡", f"訊號強度良好（平均 {rssi['mean']} dBm）",
                    "覆蓋充足", "保持現狀")

        # ── SNR ──
        if snr["mean"] is not None:
            if snr["mean"] < 15:
                add("critical", "🔇", f"訊噪比不足（平均 SNR {snr['mean']} dB）",
                    "雜訊過高，傳輸錯誤率上升、速率下降",
                    "遠離干擾源（藍牙、微波爐、無線話機），或改用 5/6 GHz 頻段", 15)
            elif snr["mean"] < 20:
                add("warning", "🔉", f"訊噪比偏低（平均 SNR {snr['mean']} dB）",
                    "高階調變（高速率）難以維持",
                    "檢查同頻道干擾，考慮換到較乾淨的頻道", 8)
            elif snr["mean"] < 25:
                add("info", "🔊", f"訊噪比尚可（平均 SNR {snr['mean']} dB）",
                    "一般應用足夠", "可微調頻道進一步改善", 3)
            else:
                add("good", "🔊", f"訊噪比良好（平均 SNR {snr['mean']} dB）",
                    "雜訊環境乾淨", "保持現狀")

        # ── 無線鏈路 ping（閘道器）──
        link = gw_stats if (gw_stats and gw_stats["sent"]) else None
        if link:
            j = link["jitter_ms"]
            l = link["loss_pct"]
            if l is not None and l >= 2:
                add("critical", "📉", f"無線鏈路掉包嚴重（{l}%）",
                    f"對閘道器 {link['host']} 掉包 {link['lost']}/{link['sent']}",
                    "掉包發生在無線端：檢查訊號強度、干擾與 AP 負載", 20)
            elif l is not None and l > 0:
                add("warning", "📉", f"無線鏈路偶有掉包（{l}%）",
                    f"對閘道器 {link['host']} 掉包 {link['lost']}/{link['sent']}",
                    "輕微掉包，觀察是否與訊號波動時段吻合", 8)
            if j is not None:
                if j > 50:
                    add("critical", "⏱", f"無線鏈路抖動過大（{j} ms）",
                        f"閘道器 RTT {link['min_ms']}–{link['max_ms']} ms",
                        "視訊通話/遊戲體驗會明顯受影響：減少同頻道干擾、"
                        "避開壅塞頻道、檢查 AP 是否過載；"
                        "若裝置啟用 Wi-Fi 省電模式也會造成延遲尖峰", 20)
                elif j > 15:
                    add("warning", "⏱", f"無線鏈路抖動偏高（{j} ms）",
                        f"閘道器 RTT {link['min_ms']}–{link['max_ms']} ms",
                        "對視訊/遊戲等即時應用有感：檢查頻道壅塞與 AP 負載", 10)
                elif j > 5:
                    add("info", "⏱", f"無線鏈路抖動略高（{j} ms）",
                        f"閘道器 RTT {link['min_ms']}–{link['max_ms']} ms",
                        "一般使用無感，重度即時應用可再優化頻道", 4)
                else:
                    add("good", "⏱", f"無線鏈路延遲穩定（抖動 {j} ms）",
                        f"閘道器平均 RTT {link['avg_ms']} ms", "保持現狀")

        # ── 對外 ping（區分 ISP 問題）──
        if inet_stats and inet_stats["sent"]:
            il = inet_stats["loss_pct"]
            ij = inet_stats["jitter_ms"]
            link_ok = (not link) or (
                (link["loss_pct"] or 0) < 0.5 and (link["jitter_ms"] or 0) <= 5)
            inet_bad = (il is not None and il >= 2) or (ij is not None and ij > 15)
            if inet_bad and link_ok:
                add("info", "🌐", "對外線路不穩，但無線鏈路正常",
                    f"對 {inet_stats['host']}：掉包 {il}%、抖動 {ij} ms；"
                    "閘道器端則正常",
                    "問題在 ISP / 對外線路，與 Wi-Fi 無關，建議聯絡網路供應商", 0)
            elif il == 0 and ij is not None and ij <= 5:
                add("good", "🌐", "對外連線品質良好",
                    f"{inet_stats['host']} 平均 RTT {inet_stats['avg_ms']} ms、"
                    f"抖動 {ij} ms、零掉包", "保持現狀")

        # ── Tx rate 波動 ──
        if tx["max"] and tx["min"] is not None and tx["max"] > 0:
            drop = (tx["max"] - tx["min"]) / tx["max"]
            if drop > 0.5:
                add("info", "🚀", f"傳輸速率波動大（{tx['min']} ～ {tx['max']} Mbps）",
                    "速率協商隨訊號品質大幅變動",
                    "通常伴隨訊號/SNR 波動，優先處理上述訊號問題", 5)

        # 總分與最嚴重發現連動：有 critical 不該顯示「非常穩定」
        if any(f["severity"] == "critical" for f in findings):
            score = min(score, 65)
        elif any(f["severity"] == "warning" for f in findings):
            score = min(score, 84)

        return self._finalize(samples, events, rssi, snr, tx,
                              gw_stats, inet_stats, gw_pings, inet_pings,
                              findings, max(0, min(100, score)))

    def _finalize(self, samples, events, rssi, snr, tx,
                  gw_stats, inet_stats, gw_pings, inet_pings,
                  findings, score) -> dict:
        first_conn = next((s for s in samples if s["connected"]), {})
        meta = getattr(self, "_meta", {}) or {}
        sev_rank = {"critical": 0, "warning": 1, "info": 2, "good": 3}
        findings.sort(key=lambda f: sev_rank.get(f["severity"], 9))
        return {
            "ok":        True,
            "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration":  self._duration,
            "ssid":      meta.get("ssid") or first_conn.get("ssid") or "—",
            "bssid":     meta.get("bssid") or first_conn.get("bssid") or "—",
            "channel":   meta.get("channel") or first_conn.get("channel"),
            "band":      meta.get("band"),
            "standard":  meta.get("standard"),
            "vendor":    meta.get("vendor"),
            "sample_count": len(samples),
            "samples":   samples,
            "rssi":      rssi,
            "snr":       snr,
            "tx":        tx,
            "ping_gw":   gw_stats,
            "ping_inet": inet_stats,
            "ping_gw_series":   gw_pings,
            "ping_inet_series": inet_pings,
            "events":    events,
            "roam_count":       sum(1 for e in events if e["type"] == "roam"),
            "disconnect_count": sum(1 for e in events if e["type"] == "disconnect"),
            "findings":  findings,
            "score":     score,
            "grade":     _grade(score),
        }


# Module-level singleton used by server.py
TEST = StabilityTest()
