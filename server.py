"""
Flask API server for Wi-Fi Survey Pro v2.1
New endpoints: /api/netperf, /api/scan/pause|resume, /api/bands,
               /api/heatmap/layers, /api/export/json
"""
import os
import sys
import json
import threading
import time
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context

import scanner
import analyzer
import heatmap_gen
import reporter
import recommender
import iperf3_mgr

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

    if not networks:
        networks = scanner.scan_networks()
    if not analysis:
        analysis = analyzer.analyze_channels(networks)

    pdf_bytes = reporter.generate_pdf(
        site_info, networks, analysis, measurements, heatmap_b64
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
    })


def _check_corewlan() -> bool:
    try:
        from CoreWLAN import CWWiFiClient
        return True
    except ImportError:
        return False
