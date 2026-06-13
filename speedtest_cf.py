"""
speedtest_cf.py — Cloudflare 公開測速端點（speed.cloudflare.com）
純 stdlib HTTP，不需安裝 Speedtest CLI：
  下載  GET  /__down?bytes=N
  上傳  POST /__up
多檔案大小漸進量測，取最佳值（接近真實吞吐）。
"""
import time
import urllib.request

_BASE    = "https://speed.cloudflare.com"
_TIMEOUT = 20
_UA      = {"User-Agent": "WiFiSurveyPro/2.2 speedtest"}


def _download_once(nbytes: int) -> float | None:
    """回傳 Mbps，失敗回 None。"""
    req = urllib.request.Request(f"{_BASE}/__down?bytes={nbytes}", headers=_UA)
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            read = 0
            while chunk := resp.read(256 * 1024):
                read += len(chunk)
        dt = time.time() - t0
        if dt <= 0 or read < nbytes * 0.9:
            return None
        return read * 8 / dt / 1e6
    except Exception:
        return None


def _upload_once(nbytes: int) -> float | None:
    data = b"\x00" * nbytes
    req = urllib.request.Request(
        f"{_BASE}/__up", data=data,
        headers={**_UA, "Content-Type": "application/octet-stream"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read()
        dt = time.time() - t0
        if dt <= 0:
            return None
        return nbytes * 8 / dt / 1e6
    except Exception:
        return None


def _latency() -> float | None:
    """同一條 keep-alive 連線上量 TTFB ×5 取中位數（ms），
    避免把 TLS 握手算進延遲。"""
    import http.client
    samples = []
    try:
        conn = http.client.HTTPSConnection("speed.cloudflare.com", timeout=8)
        # 暖身（建立 TCP+TLS，不計時）
        conn.request("GET", "/__down?bytes=0", headers=_UA)
        conn.getresponse().read()
        for _ in range(5):
            t0 = time.time()
            conn.request("GET", "/__down?bytes=0", headers=_UA)
            conn.getresponse().read()
            samples.append((time.time() - t0) * 1000)
        conn.close()
    except Exception:
        pass
    if not samples:
        return None
    samples.sort()
    return round(samples[len(samples) // 2], 1)


def run_speedtest(progress_cb=None) -> dict:
    """
    漸進式量測：先小檔暖身，速度夠快才上大檔，控制總時長 ~10-20 秒。
    progress_cb(phase: str) 可選，回報目前階段。
    """
    def report(phase):
        if progress_cb:
            try:
                progress_cb(phase)
            except Exception:
                pass

    result = {"ok": False, "down_mbps": None, "up_mbps": None,
              "latency_ms": None, "server": "Cloudflare", "error": None}

    report("latency")
    result["latency_ms"] = _latency()
    if result["latency_ms"] is None:
        result["error"] = "無法連上測速伺服器（speed.cloudflare.com）"
        return result

    # ── 下載：1MB 暖身 → 10MB → 快的話再 50MB ──
    report("download")
    down = []
    warm = _download_once(1_000_000)
    if warm:
        down.append(warm)
        mid = _download_once(10_000_000)
        if mid:
            down.append(mid)
            if mid > 80:                       # >80 Mbps 才值得測大檔
                big = _download_once(50_000_000)
                if big:
                    down.append(big)
    if down:
        result["down_mbps"] = round(max(down), 1)

    # ── 上傳：1MB 暖身 → 5MB → 快的話再 20MB ──
    report("upload")
    up = []
    warm = _upload_once(1_000_000)
    if warm:
        up.append(warm)
        mid = _upload_once(5_000_000)
        if mid:
            up.append(mid)
            if mid > 40:
                big = _upload_once(20_000_000)
                if big:
                    up.append(big)
    if up:
        result["up_mbps"] = round(max(up), 1)

    result["ok"] = result["down_mbps"] is not None or result["up_mbps"] is not None
    if not result["ok"]:
        result["error"] = "測速請求全部失敗"
    return result
