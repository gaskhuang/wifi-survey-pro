#!/usr/bin/env python3
"""
NetInspect Pro — macOS 網路現場健檢工具
Entry point: 先開視窗（載入畫面）→ 背景啟動 Flask API → 就緒後載入主畫面；
pywebview 不可用時退回系統瀏覽器。
"""
import os
import sys
import socket
import threading
import time

PORT = int(os.environ.get("WIFISURVEY_PORT", "5173"))

_LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#0f1117;color:#94a3b8;
font:14px -apple-system,BlinkMacSystemFont,"PingFang TC",sans-serif;
display:flex;align-items:center;justify-content:center}
.b{text-align:center}.t{color:#e2e8f0;font-size:18px;font-weight:700;margin-bottom:8px}
.s{width:22px;height:22px;border:2px solid #2a2f45;border-top-color:#3b82f6;border-radius:50%;
margin:14px auto 0;animation:r .8s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body><div class="b"><div class="t">NetInspect Pro</div>啟動中…<div class="s"></div></div></body></html>"""

_ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0f1117;color:#e2e8f0;
font:14px -apple-system,BlinkMacSystemFont,"PingFang TC",sans-serif;display:flex;align-items:center;justify-content:center}
.b{max-width:520px;text-align:center;line-height:1.7}.e{color:#f87171;font-weight:700;font-size:16px}
code{color:#94a3b8;font-size:12px}</style></head><body><div class="b">
<div class="e">背景服務啟動失敗</div>請關閉後重新開啟。若持續發生，請把下列記錄檔寄給技術支援：<br>
<code>{log}</code></div></body></html>"""


def _log_path() -> str:
    d = os.path.expanduser("~/Library/Logs/NetInspect Pro")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return os.devnull
    return os.path.join(d, "launch.log")


def _setup_logging():
    """從 Finder 啟動時沒有終端機，stderr 會消失 → 啟動錯誤導向記錄檔（每次啟動覆寫，不累積）。
    隱私：不記錄 HTTP 請求（路徑/參數含網段、主機等診斷資料），只留警告與錯誤。"""
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    if not sys.stderr or not sys.stderr.isatty():
        try:
            f = open(_log_path(), "w", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = f
        except OSError:
            pass


def _raise_fd_limit():
    """GUI 啟動的程序預設 fd 軟上限只有 256，LAN 掃描/埠探測並發容易耗盡（EMFILE 會讓主機被靜默漏掉）。"""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


def _find_free_port(start: int = PORT) -> int:
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def _start_flask(port: int, debug: bool = False):
    from server import app
    app.run(host="127.0.0.1", port=port, debug=debug,
            use_reloader=False, threaded=True)


def _wait_for_server(port: int, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.03)
    return False


_cleaned = threading.Event()


def _cleanup(*_a):
    """結束清理（可重複呼叫）：停監控/穩定度/流量擷取/LAN 掃描、收掉 iperf3 server。
    Cmd+Q 走 NSApplication terminate: → 直接 C exit()，Python atexit 不會執行，
    所以必須掛在視窗 closing 事件上（關視窗與 Cmd+Q 都會觸發）。回傳 None＝不取消關閉。"""
    if _cleaned.is_set():
        return None
    _cleaned.set()
    try:
        import server
        server._shutdown_cleanup()
    except Exception as e:
        print(f"[WARN] cleanup: {e}", file=sys.stderr)
    return None


def _boot(window, port, url):
    """在 GUI 執行緒之外等後端就緒，再把視窗從載入畫面切到主畫面。"""
    if _wait_for_server(port):
        print(f"NetInspect Pro running at {url}")
        window.load_url(url)
        try:                                  # 第一次開啟 → 跳定位授權（沒授權看不到 SSID/BSSID）
            import location_auth
            location_auth.request_if_needed()
        except Exception as e:
            print(f"[WARN] location auth: {e}", file=sys.stderr)
    else:
        print(f"[ERROR] Server failed to start on port {port}", file=sys.stderr)
        window.load_html(_ERROR_HTML.replace("{log}", _log_path()))


def _selftest():
    """打包驗證用：確認原生啟動程式內嵌的 Python 與 App 身分正確（讀得到定位用途說明）。"""
    import json
    from Foundation import NSBundle
    import location_auth, server  # noqa: F401  確認後端可載入
    b = NSBundle.mainBundle()
    print(json.dumps({
        "prefix": sys.prefix, "executable": sys.executable,
        "bundle": str(b.bundlePath()), "bundle_id": str(b.bundleIdentifier() or ""),
        "location": location_auth.status(),
    }, ensure_ascii=False))


def main():
    if "--selftest" in sys.argv[1:]:
        _selftest()
        return
    _setup_logging()
    _raise_fd_limit()

    port = _find_free_port()
    url  = f"http://127.0.0.1:{port}"

    # 先在背景啟動 Flask（與視窗建立並行，不再串行等待）
    t = threading.Thread(target=_start_flask, args=(port,), daemon=True)
    t.start()

    try:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), os._exit(0)))
    except Exception:
        pass

    # Try pywebview first (native macOS window)；任何失敗（含非 ImportError 的
    # WebKit/GUI 初始化錯誤）都退回瀏覽器，避免直接閃退。
    try:
        import webview  # type: ignore
    except ImportError:
        webview = None
    if webview is not None:
        try:
            win = webview.create_window(
                title     = "NetInspect Pro",
                html      = _LOADING_HTML,          # 先出現視窗，後端就緒再切主畫面
                width     = 1280,
                height    = 820,
                min_size  = (900, 600),
                frameless = False,
            )
            win.events.closing += _cleanup
            webview.start(_boot, (win, port, url), debug=False)
            _cleanup()          # 視窗正常關閉後 start() 返回 → 確保清理
            return
        except Exception as e:
            print(f"[WARN] pywebview 啟動失敗（{e}），改用瀏覽器開啟", file=sys.stderr)

    # Fallback: open in system default browser
    if not _wait_for_server(port):
        print(f"[ERROR] Server failed to start on port {port}", file=sys.stderr)
        sys.exit(1)
    import webbrowser
    webbrowser.open(url)
    print("Opened in browser. Press Ctrl+C to stop.")
    try:
        t.join()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
