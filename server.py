"""
Flask API server for Wi-Fi Survey Pro v2.1
New endpoints: /api/netperf, /api/scan/pause|resume, /api/bands,
               /api/heatmap/layers, /api/export/json
"""
import os
import sys
import json
import queue
import threading
import time
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context

import scanner
import analyzer
import heatmap_gen
import reporter
import recommender
import recommendations
import lan_scanner
import platform_caps
import config as appconfig
import ai_providers
import iperf3_mgr
import stability
import speedtest_cf
import ai_advisor

# When frozen by PyInstaller, resources live in sys._MEIPASS
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_BASE, "frontend")

app = Flask(__name__, static_folder=_FRONTEND, static_url_path="")

_monitor: scanner.SignalMonitor = scanner.SignalMonitor()
_scan_running = False  # flag for continuous scan
_scan_lock    = threading.Lock()

# ─── Static frontend ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(_FRONTEND, "index.html")

# ─── Interface selection ──────────────────────────────────────────────────────
@app.route("/api/interfaces")
def api_interfaces():
    return jsonify({"ok": True, "interfaces": scanner.get_interfaces()})

@app.route("/api/interface", methods=["POST"])
def api_set_interface():
    name = (request.get_json(silent=True) or {}).get("name", "")
    scanner.set_interface(name)
    return jsonify({"ok": True, "selected": name})

# ─── Scanner ──────────────────────────────────────────────────────────────────
@app.route("/api/scan")
def api_scan():
    nets = scanner.scan_networks()
    hidden = sum(1 for n in nets if n.get("ssid") in ("<Hidden>", "<需定位授權>"))
    return jsonify({
        "ok": True,
        "networks": nets,
        "count": len(nets),
        "needs_location": hidden > 0,
        "hidden_count": hidden,
    })


@app.route("/api/current")
def api_current():
    return jsonify(scanner.current_connection())


@app.route("/api/bands")
def api_bands():
    return jsonify({"bands": scanner.get_supported_bands()})


@app.route("/api/scan/pause", methods=["POST"])
def api_scan_pause():
    _monitor.pause()
    return jsonify({"ok": True})


@app.route("/api/scan/resume", methods=["POST"])
def api_scan_resume():
    _monitor.resume()
    return jsonify({"ok": True})


# ─── Channel analysis ─────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    nets = data.get("networks", [])
    if not nets:
        nets = scanner.scan_networks()
    result = analyzer.analyze_channels(nets)
    return jsonify({"ok": True, "analysis": result, "networks": nets})


# ─── 統一優化建議（每個功能跑完都能取得對應建議方案）──────────────────────────
@app.route("/api/recommend", methods=["POST"])
def api_recommend():
    """各分頁測試完成後呼叫，回傳聚焦該功能的優化建議方案。
    body: {context: "scan|netperf|spectrum|heatmap|monitor", ...payload}
    需要鄰居 AP / 目前連線時，後端會自行掃描補齊（除非 payload 已附帶）。"""
    data    = request.get_json(silent=True) or {}
    context = data.get("context", "scan")

    # 需要 networks 的情境：scan / spectrum（沒帶就現場掃一次）
    networks = data.get("networks")
    if networks is None and context in ("scan", "spectrum"):
        networks = scanner.scan_networks()
    networks = networks or []

    current = data.get("current")
    if current is None:
        try:
            current = scanner.current_connection() or {}
        except Exception:
            current = {}

    try:
        assessment = recommendations.assess(context, data, networks, current)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "context": context, "assessment": assessment})


# ─── Heatmap endpoints ────────────────────────────────────────────────────────
@app.route("/api/heatmap/floorplan", methods=["POST"])
def api_heatmap_floorplan():
    data = request.get_json(silent=True) or {}
    b64  = data.get("image", "")
    if not b64:
        return jsonify({"ok": False, "error": "No image data"}), 400
    return jsonify(heatmap_gen.set_floorplan(b64))


@app.route("/api/heatmap/add", methods=["POST"])
def api_heatmap_add():
    data = request.get_json(silent=True) or {}
    x    = float(data.get("x", 0))
    y    = float(data.get("y", 0))
    rssi = int(data.get("rssi", -70))
    if rssi == -70 and not data.get("rssi"):
        conn = scanner.current_connection()
        rssi = conn.get("rssi", -70)
    snr    = int(data.get("snr", 0))
    ssid   = data.get("ssid", "")
    qscore = int(data.get("quality_score", 0))
    scan_results = data.get("scan_results", [])
    return jsonify(heatmap_gen.add_measurement(x, y, rssi, snr, ssid, qscore, scan_results))


@app.route("/api/heatmap/remove/<int:point_id>", methods=["DELETE"])
def api_heatmap_remove(point_id):
    return jsonify(heatmap_gen.remove_measurement(point_id))


@app.route("/api/heatmap/points")
def api_heatmap_points():
    return jsonify({"points": heatmap_gen.get_measurements()})


@app.route("/api/heatmap/clear", methods=["POST"])
def api_heatmap_clear():
    return jsonify(heatmap_gen.clear_measurements())


@app.route("/api/heatmap/generate")
def api_heatmap_generate():
    w       = int(request.args.get("w", 1000))
    h       = int(request.args.get("h", 700))
    labels  = request.args.get("labels", "1") == "1"
    metric  = request.args.get("metric", "rssi")   # rssi | snr | quality
    contour = request.args.get("contour", "1") == "1"
    img_b64 = heatmap_gen.generate_heatmap(w, h, labels, metric, contour)
    return jsonify({"ok": bool(img_b64), "image": img_b64, "metric": metric})


@app.route("/api/heatmap/export_csv")
def api_heatmap_csv():
    csv_data = heatmap_gen.export_csv()
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=wifi_measurements.csv"})


@app.route("/api/heatmap/export_json")
def api_heatmap_json():
    return Response(heatmap_gen.export_json(), mimetype="application/json",
                    headers={"Content-Disposition": "attachment;filename=wifi_measurements.json"})


# ─── Live monitor ─────────────────────────────────────────────────────────────
@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    data     = request.get_json(silent=True) or {}
    ssid     = data.get("ssid")
    interval = float(data.get("interval", 2.0))
    _monitor.target_ssid = ssid
    _monitor.interval    = interval
    _monitor.clear()
    _monitor.start()
    return jsonify({"ok": True})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    _monitor.stop()
    return jsonify({"ok": True})


@app.route("/api/monitor/data")
def api_monitor_data():
    return jsonify({"points": _monitor.get_data()})


@app.route("/api/monitor/stream")
def api_monitor_stream():
    def event_gen():
        last_len = 0
        while True:
            pts = _monitor.get_data()
            if len(pts) > last_len:
                for pt in pts[last_len:]:
                    yield f"data: {json.dumps(pt)}\n\n"
                last_len = len(pts)
            time.sleep(0.5)
    return Response(event_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Network performance test ─────────────────────────────────────────────────
@app.route("/api/netperf")
def api_netperf():
    """Run ping/DNS/HTTP latency tests (no iperf3 server needed)."""
    target  = request.args.get("host", "8.8.8.8")
    dns     = request.args.get("dns", "8.8.8.8")
    samples = int(request.args.get("n", 5))
    # Run in thread with timeout
    result: dict = {}
    done_event = threading.Event()

    def run():
        result.update(scanner.test_network_performance(target, dns, samples))
        done_event.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done_event.wait(timeout=30)
    return jsonify({"ok": True, **result})


# ─── Export endpoints ─────────────────────────────────────────────────────────
@app.route("/api/export/networks")
def api_export_networks():
    """Export scan results in NETworkManager-compatible CSV format."""
    nets = scanner.scan_networks()
    if not nets:
        return Response("No data", mimetype="text/plain"), 404
    # NETworkManager CSV schema
    header = "Bssid,Ssid,Channel,Band,ChannelWidth,Rssi,Snr,Noise,PhyKind,Security,Vendor,QualityScore,StationCount,ChannelUtil%,IsNew,FirstSeen,LastSeen"
    rows   = [header]
    for n in nets:
        rows.append(",".join(str(n.get(k, "")) for k in [
            "bssid", "ssid", "channel", "band", "width", "rssi", "snr", "noise",
            "standard", "security", "vendor", "quality_score",
            "station_count", "channel_util_pct", "is_new",
            "first_seen", "last_seen",
        ]))
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Response("\n".join(rows), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=wifi_scan_{ts}.csv"})


@app.route("/api/export/json")
def api_export_json():
    nets     = scanner.scan_networks()
    analysis = analyzer.analyze_channels(nets)
    payload  = {
        "version": "2.0",
        "timestamp": time.time(),
        "networks": nets,
        "analysis": analysis,
    }
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Response(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    mimetype="application/json",
                    headers={"Content-Disposition": f"attachment;filename=wifi_scan_{ts}.json"})


# ─── Report ───────────────────────────────────────────────────────────────────
@app.route("/api/report", methods=["POST"])
def api_report():
    data         = request.get_json(silent=True) or {}
    site_info    = data.get("site_info", {})
    networks     = data.get("networks", [])
    analysis     = data.get("analysis", {})
    measurements = heatmap_gen.get_measurements()
    heatmap_b64  = data.get("heatmap", "")
    spectrum_b64 = data.get("spectrum", "")

    if not networks:
        networks = scanner.scan_networks()
    if not analysis:
        analysis = analyzer.analyze_channels(networks)

    pdf_bytes = reporter.generate_pdf(
        site_info, networks, analysis, measurements, heatmap_b64, spectrum_b64
    )
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename=wifi_survey_{ts}.pdf"}
    )


# ─── iperf3 endpoints ────────────────────────────────────────────────────────
@app.route("/api/iperf/check")
def api_iperf_check():
    info = iperf3_mgr.check_installed()
    info["local_ips"] = iperf3_mgr.get_local_ips()
    return jsonify(info)


@app.route("/api/iperf/server/start", methods=["POST"])
def api_iperf_server_start():
    data = request.get_json(silent=True) or {}
    port = int(data.get("port", 5201))
    return jsonify(iperf3_mgr.get_server().start(port))


@app.route("/api/iperf/server/stop", methods=["POST"])
def api_iperf_server_stop():
    return jsonify(iperf3_mgr.get_server().stop())


@app.route("/api/iperf/server/status")
def api_iperf_server_status():
    return jsonify(iperf3_mgr.get_server().status())


@app.route("/api/iperf/run", methods=["POST"])
def api_iperf_run():
    data     = request.get_json(silent=True) or {}
    target   = data.get("target", "127.0.0.1")
    port     = int(data.get("port", 5201))
    duration = int(data.get("duration", 10))
    mode     = data.get("mode", "tcp_dl")
    streams  = int(data.get("streams", 1))

    result: dict = {}
    done   = threading.Event()
    def _run():
        result.update(iperf3_mgr.run_test(target, port, duration, mode, streams))
        done.set()

    threading.Thread(target=_run, daemon=True).start()
    done.wait(timeout=duration + 20)
    return jsonify(result if result else {"ok": False, "error": "Test timed out"})


@app.route("/api/iperf/stream")
def api_iperf_stream():
    """SSE endpoint — streams iperf3 output line by line + final analysis."""
    target   = request.args.get("target", "127.0.0.1")
    port     = int(request.args.get("port", 5201))
    duration = int(request.args.get("duration", 10))
    mode     = request.args.get("mode", "tcp_dl")
    streams  = int(request.args.get("streams", 1))

    def generate():
        for event_type, data in iperf3_mgr.stream_test(target, port, duration, mode, streams):
            payload = json.dumps(data, ensure_ascii=False)
            yield f"event: {event_type}\ndata: {payload}\n\n"
        yield "event: done\ndata: {}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── One-tap diagnose pipeline (SSE) ─────────────────────────────────────────
@app.route("/api/diagnose/stream")
def api_diagnose_stream():
    """
    一鍵診斷完整管線（SSE）：
    掃描 → 頻道分析 → 連線狀態 → Ping/DNS → 速度測試 → 穩定度 → AI 建議
    事件：step（進度）/ partial（各區結果）/ done（完整彙整）
    """
    duration = max(10, min(120, int(request.args.get("duration", 30))))
    do_speed = request.args.get("speedtest", "1") == "1"
    do_ai    = request.args.get("ai", "1") == "1"

    def ev(etype, data):
        return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def gen():
        results: dict = {}

        def run_step(key, label, fn):
            yield ev("step", {"key": key, "label": label, "status": "running"})
            try:
                results[key] = fn()
                yield ev("step", {"key": key, "label": label, "status": "ok"})
                yield ev("partial", {"key": key, "data": results[key]})
            except Exception as e:
                results[key] = None
                yield ev("step", {"key": key, "label": label,
                                  "status": "fail", "error": str(e)})

        # 1-4: 快速項目
        yield from run_step("scan",    "Wi-Fi 環境掃描",        scanner.scan_networks)
        yield from run_step("analysis","頻道分析",
                            lambda: analyzer.analyze_channels(results.get("scan") or []))
        yield from run_step("current", "目前連線狀態",          scanner.current_connection)
        yield from run_step("netperf", "Ping / DNS 延遲測試",
                            lambda: scanner.test_network_performance("8.8.8.8", "8.8.8.8", 5))

        # 5: 速度測試
        if do_speed:
            yield from run_step("speedtest", "網路速度測試（上傳/下載）",
                                speedtest_cf.run_speedtest)

        # 6: 穩定度（背景跑 + 進度回報）
        yield ev("step", {"key": "stability", "label": f"連線穩定度測試（{duration} 秒）",
                          "status": "running"})
        if stability.TEST.start(duration):
            while True:
                st = stability.TEST.status()
                if not st["running"]:
                    break
                yield ev("progress", {"key": "stability",
                                      "elapsed": st["elapsed"],
                                      "total":   st["duration"]})
                time.sleep(2)
            results["stability"] = stability.TEST.result_data
            yield ev("step", {"key": "stability", "label": "連線穩定度測試",
                              "status": "ok"})
            yield ev("partial", {"key": "stability", "data": results["stability"]})
        else:
            yield ev("step", {"key": "stability", "label": "連線穩定度測試",
                              "status": "fail", "error": "已有穩定度測試進行中"})

        # 7: AI 分析（背景執行 + 心跳，讓前端顯示經過時間）
        if do_ai:
            provider_hint = "Codex AI" if ai_advisor.codex_available() else "離線規則引擎"
            yield ev("step", {"key": "ai", "label": f"AI 智慧分析（{provider_hint}）",
                              "status": "running"})
            holder: dict = {}
            def _ai():
                holder["r"] = ai_advisor.analyze(
                    results.get("scan") or [],
                    results.get("analysis") or {},
                    results.get("current") or {},
                    results.get("netperf") or {},
                    results.get("speedtest"),
                    results.get("stability"))
            t = threading.Thread(target=_ai, daemon=True)
            t.start()
            waited = 0
            while t.is_alive() and waited < ai_advisor.CODEX_TIMEOUT + 30:
                time.sleep(2)
                waited += 2
                yield ev("progress", {"key": "ai", "elapsed": waited})
            ai_result = holder.get("r") or {"ok": False, "provider": "none",
                                            "summary": "", "score": 0, "items": []}
            results["ai"] = ai_result
            yield ev("step", {"key": "ai", "label": "AI 智慧分析",
                              "status": "ok" if ai_result.get("ok") else "fail"})
            yield ev("partial", {"key": "ai", "data": ai_result})

        yield ev("done", {"ok": True})

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/diagnose/report", methods=["POST"])
def api_diagnose_report():
    """把現場診斷結果輸出成 PDF 報告。
    body: diagnose 分頁蒐集的結果（current/netperf/speedtest/stability/analysis/scan/ai）
          + 選填 site_info。"""
    data      = request.get_json(silent=True) or {}
    site_info = data.pop("site_info", {}) if isinstance(data, dict) else {}
    pdf_bytes = reporter.generate_diagnosis_pdf(data, site_info)
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename=wifi_diagnosis_{ts}.pdf"})


# ─── 平台能力偵測 ─────────────────────────────────────────────────────────────
@app.route("/api/capabilities")
def api_capabilities():
    return jsonify(platform_caps.capabilities())


# ─── 設定（AI 供應商 / 隱私 / 白標）────────────────────────────────────────────
@app.route("/api/settings")
def api_settings_get():
    return jsonify({"ok": True, "settings": appconfig.public_view()})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    patch = request.get_json(silent=True) or {}
    # 安全：ai.keys 中的空字串視為「不變更」，避免把已存金鑰洗掉
    try:
        keys = patch.get("ai", {}).get("keys")
        if isinstance(keys, dict):
            patch["ai"]["keys"] = {k: v for k, v in keys.items() if v}
        cloud = patch.get("ai", {}).get("cloud")
        if isinstance(cloud, dict) and cloud.get("token") == "":
            cloud.pop("token", None)
    except Exception:
        pass
    appconfig.save(patch)
    return jsonify({"ok": True, "settings": appconfig.public_view()})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    data = request.get_json(silent=True) or {}
    provider = data.get("provider") or appconfig.get("ai.provider", "offline")
    return jsonify(ai_providers.test(provider))


# ─── LAN 有線盤查 ─────────────────────────────────────────────────────────────
_LAST_LAN = {}      # 最近一次掃描結果（供 result / export 使用）


def _sse(etype, data):
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.route("/api/lan/interfaces")
def api_lan_interfaces():
    return jsonify({"interfaces": lan_scanner.list_interfaces()})


def _validate_subnet(subnet: str):
    """回傳 (正規化網段, 錯誤訊息)。限 IPv4、私網、大小合理。"""
    import ipaddress
    if not subnet:
        ifs = lan_scanner.list_interfaces()
        return (ifs[0]["subnet_cidr"] if ifs else "192.168.1.0/24"), None
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None, "網段格式不正確（例：192.168.1.0/24）"
    if net.version != 4:
        return None, "目前僅支援 IPv4 網段"
    if not (net.is_private or net.is_loopback):
        return None, "為安全起見僅允許掃描私有網段（10/8、172.16/12、192.168/16）"
    if net.num_addresses > lan_scanner.MAX_ADDRS:
        return None, f"網段過大，請改用 ≤{lan_scanner.MAX_ADDRS} 位址（/20 以上）的網段"
    return str(net), None


@app.route("/api/lan/scan/stream")
def api_lan_scan_stream():
    """SSE：掃描網段。事件 progress {done,total,host?} / done {result} / error。"""
    subnet, err = _validate_subnet(request.args.get("subnet", "").strip())
    deep = request.args.get("deep", "1") == "1"

    q: "queue.Queue" = queue.Queue()
    stop_evt = threading.Event()

    def prog(done, total, host):
        if host is not None:
            q.put(("progress", {"done": done, "total": total, "host": host}))
        elif done == total or done % 8 == 0:
            q.put(("progress", {"done": done, "total": total, "host": None}))

    def run():
        global _LAST_LAN
        try:
            r = lan_scanner.scan(subnet, deep=deep, progress=prog,
                                 should_stop=stop_evt.is_set)
            if not stop_evt.is_set():
                _LAST_LAN = r
                q.put(("done", r))
        except Exception as e:
            q.put(("error", {"error": str(e)}))
        q.put((None, None))

    if err:
        # 參數錯誤：直接回一個只含 error 事件的短串流（前端統一以 error 事件處理）
        def gen_err():
            yield _sse("error", {"error": err})
        return Response(stream_with_context(gen_err()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    threading.Thread(target=run, daemon=True).start()

    def gen():
        try:
            yield _sse("start", {"subnet": subnet, "deep": deep})
            while True:
                etype, data = q.get()
                if etype is None:
                    break
                yield _sse(etype, data)
        finally:
            stop_evt.set()      # 客戶端斷線 → 通知背景掃描盡快停止，避免孤兒掃描堆疊

    return Response(
        stream_with_context(gen()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lan/result")
def api_lan_result():
    return jsonify({"ok": bool(_LAST_LAN), "result": _LAST_LAN})


@app.route("/api/lan/ping", methods=["POST"])
def api_lan_ping():
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    if not lan_scanner.is_valid_ipv4(ip):
        return jsonify({"ok": False, "error": "IP 格式不正確"}), 400
    try:
        count = max(1, min(10, int(data.get("count", 4))))
    except (ValueError, TypeError):
        count = 4
    return jsonify({"ok": True, "result": lan_scanner.ping(ip, count)})


@app.route("/api/lan/portscan", methods=["POST"])
def api_lan_portscan():
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    if not lan_scanner.is_valid_ipv4(ip):
        return jsonify({"ok": False, "error": "IP 格式不正確"}), 400
    return jsonify({"ok": True, "result": lan_scanner.portscan(ip)})


@app.route("/api/lan/export")
def api_lan_export():
    if not _LAST_LAN:
        return Response("尚無掃描結果", mimetype="text/plain"), 404
    fmt = request.args.get("fmt", "csv")
    ts  = time.strftime("%Y%m%d_%H%M%S")
    if fmt == "json":
        return Response(
            json.dumps(_LAST_LAN, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment;filename=lan_inventory_{ts}.json"})
    return Response(
        lan_scanner.to_csv(_LAST_LAN), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=lan_inventory_{ts}.csv"})


# ─── Connection stability test ──────────────────────────────────────────────
@app.route("/api/stability/start", methods=["POST"])
def api_stability_start():
    data     = request.get_json(silent=True) or {}
    duration = int(data.get("duration", 60))
    host     = data.get("host", "8.8.8.8")
    ok = stability.TEST.start(duration, host)
    return jsonify({"ok": ok, "error": None if ok else "測試進行中，請稍候"})


@app.route("/api/stability/stop", methods=["POST"])
def api_stability_stop():
    stability.TEST.stop()
    return jsonify({"ok": True})


@app.route("/api/stability/status")
def api_stability_status():
    return jsonify(stability.TEST.status())


@app.route("/api/stability/result")
def api_stability_result():
    r = stability.TEST.result_data
    return jsonify({"ok": bool(r), "result": r})


@app.route("/api/stability/report", methods=["POST"])
def api_stability_report():
    data = request.get_json(silent=True) or {}
    r = stability.TEST.result_data
    if not r:
        return jsonify({"ok": False, "error": "尚無測試結果，請先執行穩定度分析"}), 400
    pdf_bytes = reporter.generate_stability_pdf(
        r, data.get("chart", ""), data.get("site_info", {}))
    ts = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename=wifi_stability_{ts}.pdf"})


# ─── Full diagnostic test ────────────────────────────────────────────────────
@app.route("/api/full-test", methods=["POST"])
def api_full_test():
    """
    Run all tests in sequence and return structured recommendations.
    Steps: scan → analyze → netperf → current → assess
    """
    progress = []

    def step(name, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            progress.append({"step": name, "ok": True})
            return result
        except Exception as e:
            progress.append({"step": name, "ok": False, "error": str(e)})
            return None

    # Step 1: Scan
    networks = step("Wi-Fi 環境掃描", scanner.scan_networks) or []

    # Step 2: Channel analysis
    analysis = step("頻道分析", analyzer.analyze_channels, networks) or {}

    # Step 3: Current connection
    current = step("目前連線狀態", scanner.current_connection) or {}

    # Step 4: Network performance
    netperf = step("網路效能測試（Ping/DNS）", scanner.test_network_performance, "8.8.8.8", "8.8.8.8", 5) or {}

    # Step 5: Get existing heatmap measurements
    measurements = heatmap_gen.get_measurements()
    progress.append({"step": f"覆蓋量測記錄（{len(measurements)} 點）", "ok": True})

    # Step 6: Generate recommendations
    assessment = recommender.full_assessment(networks, analysis, current, netperf, measurements)

    return jsonify({
        "ok":          True,
        "progress":    progress,
        "networks":    networks,
        "analysis":    analysis,
        "current":     current,
        "netperf":     netperf,
        "assessment":  assessment,
    })


# ─── Info ─────────────────────────────────────────────────────────────────────
@app.route("/api/info")
def api_info():
    import platform, sys
    return jsonify({
        "app":      "Wi-Fi Survey Pro",
        "version":  "2.1.0",
        "python":   sys.version.split()[0],
        "platform": platform.platform(),
        "corewlan": _check_corewlan(),
        "bands":    scanner.get_supported_bands(),
        "ai_provider": "codex" if ai_advisor.codex_available() else "rules",
    })


def _check_corewlan() -> bool:
    try:
        from CoreWLAN import CWWiFiClient
        return True
    except ImportError:
        return False
