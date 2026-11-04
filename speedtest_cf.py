"""
speedtest_cf.py — Cloudflare 公開測速端點（speed.cloudflare.com）
純 stdlib HTTP，不需安裝 Speedtest CLI：
  下載  GET  /__down?bytes=N
  上傳  POST /__up
多檔案大小漸進量測，取最佳值（接近真實吞吐）。
"""
import http.client
import time
import urllib.parse

_BASE    = "https://speed.cloudflare.com"
_TIMEOUT = 20          # 單一 socket 操作上限（連線 / 單次 recv）
_DIR_BUDGET = 12.0     # 每個方向（下載 / 上傳）的總時間預算（秒）
_CHUNK   = 64 * 1024
_MIN_PARTIAL_BYTES = 128 * 1024   # 逾時時至少要傳這麼多才採計部分結果
_UA      = {"User-Agent": "WiFiSurveyPro/2.2 speedtest"}


def _conn(deadline: float):
    """建立到 _BASE 的連線；TCP+TLS 在這裡完成，不計入吞吐計時。"""
    u = urllib.parse.urlsplit(_BASE)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    left = deadline - time.monotonic()
    if left <= 0.2:
        raise TimeoutError("budget exhausted")
    c = cls(u.hostname, u.port, timeout=min(_TIMEOUT, left))
    c.connect()
    return c


def _set_timeout(c, deadline: float) -> bool:
    """把 socket 逾時縮到剩餘預算內；預算用完回 False。"""
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    try:
        c.sock.settimeout(max(0.2, min(_TIMEOUT, left)))
    except Exception:
        pass
    return True


def _unsent(c) -> int:
    """送出緩衝區中尚未被對方確認的位元組（best-effort）：macOS SO_NWRITE、
    Linux SIOCOUTQ；取不到回 0。上傳逾時估速率時扣掉，避免把本機緩衝算成吞吐。"""
    try:
        import platform, socket
        sysname = platform.system()
        if sysname == "Darwin":
            return max(0, c.sock.getsockopt(socket.SOL_SOCKET, 0x1024))   # SO_NWRITE
        if sysname == "Linux":
            import array, fcntl
            buf = array.array("i", [0])
            fcntl.ioctl(c.sock.fileno(), 0x5411, buf)                     # SIOCOUTQ
            return max(0, buf[0])
    except Exception:
        pass
    return 0


def _rate(nbytes: int, dt: float) -> float | None:
    if dt <= 0 or nbytes <= 0:
        return None
    return nbytes * 8 / dt / 1e6


def _download_once(nbytes: int, deadline: float | None = None) -> tuple[float | None, bool]:
    """回傳 (Mbps, partial)。計時從第一個 body byte 開始（排除 TLS/TTFB）；
    超過 deadline 就停，以目前已收的位元組估速率（partial=True）。失敗回 (None, False)。"""
    if deadline is None:
        deadline = time.monotonic() + _DIR_BUDGET
    c = None
    try:
        c = _conn(deadline)
        c.request("GET", f"/__down?bytes={nbytes}", headers=_UA)
        resp = c.getresponse()
        if resp.status != 200:
            return None, False
        read, total, t0, t_last, partial = 0, 0, None, None, False
        while True:
            if not _set_timeout(c, deadline):
                partial = True
                break
            try:
                chunk = resp.read1(_CHUNK)
            except TimeoutError:
                partial = True
                break
            if not chunk:
                break
            now = time.monotonic()
            total += len(chunk)
            if t0 is None:
                t0 = now                 # 第一個 byte 到達才開始計時
            else:
                read += len(chunk)       # 第一塊只當起點，不計入（避免高估）
            t_last = now
        if t0 is None or t_last is None:
            return None, False
        if partial:
            if read < _MIN_PARTIAL_BYTES:
                return None, False
            return _rate(read, t_last - t0), True
        if total < nbytes * 0.9:
            return None, False
        return _rate(read, t_last - t0), False
    except Exception:
        return None, False
    finally:
        try:
            c and c.close()
        except Exception:
            pass


def _upload_once(nbytes: int, deadline: float | None = None) -> tuple[float | None, bool]:
    """回傳 (Mbps, partial)。連線/TLS 先建好再開始計時；超過 deadline 就停止送出，
    以已送出的位元組估速率（partial=True）。"""
    if deadline is None:
        deadline = time.monotonic() + _DIR_BUDGET
    c = None
    try:
        c = _conn(deadline)
        c.putrequest("POST", "/__up")
        for k, v in {**_UA, "Content-Type": "application/octet-stream"}.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(nbytes))
        c.endheaders()
        block = b"\x00" * _CHUNK
        sent, partial = 0, False
        t0 = time.monotonic()
        while sent < nbytes:
            if not _set_timeout(c, deadline):
                partial = True
                break
            n = min(_CHUNK, nbytes - sent)
            try:
                c.send(block if n == _CHUNK else block[:n])
            except TimeoutError:
                partial = True
                break
            sent += n
        if partial:
            dt = time.monotonic() - t0
            acked = sent - _unsent(c)
            if acked < _MIN_PARTIAL_BYTES:
                return None, False
            return _rate(acked, dt), True
        # 全部送完：等伺服器回應，確認資料真的送達（不只是進了本機 socket buffer）
        if not _set_timeout(c, deadline + 2):
            return _rate(sent - _unsent(c), time.monotonic() - t0), True
        try:
            resp = c.getresponse()
            resp.read()
        except TimeoutError:
            # 資料都進了 socket buffer 但對方遲遲未收完：以已確認送達的量估速率
            acked = sent - _unsent(c)
            if acked < _MIN_PARTIAL_BYTES:
                return None, False
            return _rate(acked, time.monotonic() - t0), True
        dt = time.monotonic() - t0
        if resp.status >= 400:
            return None, False
        return _rate(sent, dt), False
    except Exception:
        return None, False
    finally:
        try:
            c and c.close()
        except Exception:
            pass


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


def _progressive(fn, sizes: tuple, big_threshold: float) -> tuple[list, bool]:
    """暖身 → 中檔 → （夠快才）大檔；全部共用一個方向的總預算。
    任何一段撞到預算就採用已傳輸量算出的速率並停止後續階段。"""
    deadline = time.monotonic() + _DIR_BUDGET
    rates: list[float] = []
    warm_sz, mid_sz, big_sz = sizes
    r, partial = fn(warm_sz, deadline)
    if r:
        rates.append(r)
    if not r or partial:
        return rates, partial
    r, partial = fn(mid_sz, deadline)
    if r:
        rates.append(r)
    if not r or partial or r <= big_threshold:   # 夠快才值得測大檔
        return rates, partial
    r, partial = fn(big_sz, deadline)
    if r:
        rates.append(r)
    return rates, partial


def run_speedtest(progress_cb=None) -> dict:
    """
    漸進式量測：先小檔暖身，速度夠快才上大檔；下載/上傳各有 _DIR_BUDGET 秒總預算，
    超時以已傳輸量估速率（down_partial / up_partial = True）。
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

    # ── 下載：1MB 暖身 → 10MB → 快的話再 50MB（整個方向共用 _DIR_BUDGET 秒）──
    report("download")
    down, down_partial = _progressive(
        _download_once, (1_000_000, 10_000_000, 50_000_000), 80)
    if down:
        result["down_mbps"] = round(max(down), 1)
    result["down_partial"] = down_partial

    # ── 上傳：1MB 暖身 → 5MB → 快的話再 20MB ──
    report("upload")
    up, up_partial = _progressive(
        _upload_once, (1_000_000, 5_000_000, 20_000_000), 40)
    if up:
        result["up_mbps"] = round(max(up), 1)
    result["up_partial"] = up_partial

    result["ok"] = result["down_mbps"] is not None or result["up_mbps"] is not None
    if not result["ok"]:
        result["error"] = "測速請求全部失敗"
    return result
