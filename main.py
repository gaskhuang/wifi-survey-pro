#!/usr/bin/env python3
"""
NetInspect Pro — macOS Wi-Fi 現場勘測工具
Entry point: launches Flask API + opens pywebview window (or browser fallback)
"""
import os
import sys
import socket
import threading
import time

PORT = int(os.environ.get("WIFISURVEY_PORT", "5173"))


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


def _wait_for_server(port: int, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.03)
    return False


def main():
    port = _find_free_port()
    url  = f"http://127.0.0.1:{port}"

    # Start Flask in background thread
    t = threading.Thread(target=_start_flask, args=(port,), daemon=True)
    t.start()

    if not _wait_for_server(port):
        print(f"[ERROR] Server failed to start on port {port}", file=sys.stderr)
        sys.exit(1)

    print(f"NetInspect Pro running at {url}")

    # Try pywebview first (native macOS window)；任何失敗（含非 ImportError 的
    # WebKit/GUI 初始化錯誤）都退回瀏覽器，避免直接閃退。
    try:
        import webview  # type: ignore
    except ImportError:
        webview = None
    if webview is not None:
        try:
            webview.create_window(
                title     = "NetInspect Pro",
                url       = url,
                width     = 1280,
                height    = 820,
                min_size  = (900, 600),
                frameless = False,
            )
            webview.start(debug=False)
            return
        except Exception as e:
            print(f"[WARN] pywebview 啟動失敗（{e}），改用瀏覽器開啟", file=sys.stderr)

    # Fallback: open in system default browser
    import webbrowser
    webbrowser.open(url)
    print("Opened in browser. Press Ctrl+C to stop.")
    try:
        t.join()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
