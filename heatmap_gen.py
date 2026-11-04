"""
Heatmap generator — RBF interpolation（純 numpy，線性核 phi(r)=r，等同 scipy Rbf linear）
Supports multiple metric layers: RSSI, SNR, quality score
Corner padding prevents boundary artifacts (from python-wifi-survey-heatmap)
Stores full AP scan per measurement point for channel utilization layer
"""
import base64
import io
import itertools
import threading
import time
from typing import Optional

# Session state
# id 用遞增計數器：舊版 len(_measurements) 在刪除後會產生重複 id（刪一個掉兩點）
_id_seq = itertools.count()
_lock = threading.RLock()        # 保護 _measurements（Flask threaded 併發請求）
_measurements: list[dict] = []
_floorplan_b64: Optional[str] = None
_floorplan_size: tuple[int, int] = (0, 0)


def set_floorplan(b64_data: str) -> dict:
    global _floorplan_b64, _floorplan_size
    try:
        from PIL import Image
        img_bytes = base64.b64decode(b64_data.split(",")[-1])
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        with _lock:
            _floorplan_b64 = b64_data
            _floorplan_size = img.size
        return {"ok": True, "width": img.size[0], "height": img.size[1]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def add_measurement(x_pct: float, y_pct: float, rssi: int,
                    snr: int = 0, ssid: str = "",
                    quality_score: int = 0,
                    scan_results: list = None) -> dict:
    """
    x_pct, y_pct  — position as percentage 0.0–1.0
    scan_results  — optional full AP scan at this point [{ssid,bssid,rssi,channel,band}]
    """
    pt = {
        "id":            next(_id_seq),
        "x":             x_pct,
        "y":             y_pct,
        "rssi":          rssi,
        "snr":           snr,
        "ssid":          ssid,
        "quality_score": quality_score,
        "scan_results":  scan_results or [],
        "ts":            time.time(),
        "label":         _rssi_label(rssi),
        "color":         _rssi_color(rssi),
    }
    with _lock:
        _measurements.append(pt)
        total = len(_measurements)
    return {"ok": True, "point": pt, "total": total}


def remove_measurement(point_id: int) -> dict:
    global _measurements
    with _lock:
        _measurements = [p for p in _measurements if p["id"] != point_id]
        total = len(_measurements)
    return {"ok": True, "total": total}


def get_measurements() -> list[dict]:
    with _lock:
        return list(_measurements)


def clear_measurements() -> dict:
    global _measurements, _id_seq
    with _lock:
        _measurements = []
        _id_seq = itertools.count()   # 已無任何點，可安全歸零（前端顯示 #id+1）
    return {"ok": True}


def _pair_dist(ax, ay, bx, by):
    """兩組點的歐氏距離矩陣 (len(a), len(b))。"""
    import numpy as np
    return np.hypot(ax[:, None] - bx[None, :], ay[:, None] - by[None, :])


def _rbf_linear(xs, ys, vals, gx, gy, chunk: int = 16384):
    """線性 RBF 插值（phi(r)=r，smooth=0），與 scipy.interpolate.Rbf(function="linear")
    數值一致（實測最大相對誤差 < 1e-12）；省掉 scipy 依賴。
    weights = solve(dist(P,P), vals)；field = dist(G,P) @ weights，分塊避免大暫存。"""
    import numpy as np
    A = _pair_dist(xs, ys, xs, ys)
    try:
        w = np.linalg.solve(A, vals)
    except np.linalg.LinAlgError:
        # 同一位置重複量測 → 奇異矩陣；改用最小平方解而非整張圖失敗
        w = np.linalg.lstsq(A, vals, rcond=None)[0]
    fx, fy = gx.ravel(), gy.ravel()
    out = np.empty(fx.size)
    for i in range(0, fx.size, chunk):
        out[i:i + chunk] = _pair_dist(fx[i:i + chunk], fy[i:i + chunk], xs, ys) @ w
    return out.reshape(gx.shape)


def _min_dist_lowres(px, py, width: int, height: int, step: int = 4,
                     chunk: int = 16384):
    """在 1/step 解析度格點上算「到最近量測點的距離」，再雙線性放大回全尺寸。
    舊版逐點對全解析度陣列求距離，N=150、1000×700 時佔整體 ~75% 時間。"""
    import numpy as np
    from PIL import Image as PILImg
    sw, sh = max(1, width // step), max(1, height // step)
    # 小圖像素中心對應的全尺寸座標（與 PIL resize 的像素中心對齊方式一致）
    cx = (np.arange(sw) + 0.5) * (width / sw) - 0.5
    cy = (np.arange(sh) + 0.5) * (height / sh) - 0.5
    gx, gy = np.meshgrid(cx, cy)
    fx, fy = gx.ravel(), gy.ravel()
    md = np.empty(fx.size, dtype=np.float32)
    for i in range(0, fx.size, chunk):
        md[i:i + chunk] = _pair_dist(fx[i:i + chunk], fy[i:i + chunk], px, py).min(axis=1)
    small = PILImg.fromarray(md.reshape(sh, sw), mode="F")
    return np.asarray(small.resize((width, height), PILImg.BILINEAR), dtype=np.float32)


def generate_heatmap(width: int = 1000, height: int = 700,
                     show_labels: bool = True,
                     metric: str = "rssi",   # "rssi" | "snr" | "quality"
                     show_contours: bool = True) -> str:
    """
    Render multi-metric heatmap on floor plan.
    Uses RBF interpolation with corner padding.
    Returns base64-encoded PNG data URI.
    show_contours：保留參數相容呼叫端；等高線依賴的 matplotlib 未安裝（一直被靜默略過），
    已移除該死碼，此參數目前不作用。
    """
    with _lock:
        pts = list(_measurements)       # 快照：渲染期間不受併發新增/刪除影響
        floorplan_b64 = _floorplan_b64
    if len(pts) < 2:
        return ""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
    except ImportError:
        return _fallback_heatmap(width, height)

    # ── Extract coordinates and values ───────────────────────────────────
    xs  = np.array([p["x"] * width  for p in pts], dtype=float)
    ys  = np.array([p["y"] * height for p in pts], dtype=float)

    if metric == "snr":
        vals = np.array([p["snr"]           for p in pts], dtype=float)
        vmin, vmax = 0, 50
    elif metric == "quality":
        vals = np.array([p["quality_score"] for p in pts], dtype=float)
        vmin, vmax = 0, 100
    else:  # rssi
        vals = np.array([p["rssi"]          for p in pts], dtype=float)
        vmin, vmax = -95, -30

    # ── Corner padding (prevents boundary artifacts) ─────────────────────
    # Add 4 corner points at min value so RBF extrapolates to "bad signal"
    pad_val = vals.min()
    xs  = np.append(xs,  [0, width, 0,     width])
    ys  = np.append(ys,  [0, 0,    height, height])
    vals = np.append(vals, [pad_val] * 4)

    # ── RBF interpolation (Radial Basis Function — better than IDW) ───────
    try:
        gx, gy = np.meshgrid(np.linspace(0, width, width // 4),
                              np.linspace(0, height, height // 4))
        grid_z = _rbf_linear(xs, ys, vals, gx, gy)
        grid_z = np.clip(grid_z, vmin, vmax)
        # Upsample to full resolution
        from PIL import Image as PILImg
        grid_img = ((grid_z - vmin) / (vmax - vmin) * 255).astype(np.uint8)
        small = PILImg.fromarray(grid_img, mode="L")
        norm_full = np.array(small.resize((width, height), PILImg.BILINEAR)) / 255.0
    except Exception:
        return _fallback_heatmap(width, height)

    # ── Map normalized value → RGBA ───────────────────────────────────────
    rgba = _apply_colormap(norm_full)

    # ── Alpha mask: fade at edges (proximity-based) ───────────────────────
    xs_pts = xs[:-4]  # exclude corners
    ys_pts = ys[:-4]
    min_dists = _min_dist_lowres(xs_pts, ys_pts, width, height)
    falloff = min(width, height) * 0.15
    alpha_f = np.clip(1.0 - (min_dists - falloff) / (falloff * 0.6), 0, 1)
    rgba[:, :, 3] = (alpha_f * 150).astype(np.uint8)

    heat_img = Image.fromarray(rgba, "RGBA")

    # ── Base image ────────────────────────────────────────────────────────
    if floorplan_b64:
        img_bytes = base64.b64decode(floorplan_b64.split(",")[-1])
        base_img  = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        base_img  = base_img.resize((width, height), Image.LANCZOS)
    else:
        base_img = Image.new("RGBA", (width, height), (25, 28, 42, 255))

    composite = Image.alpha_composite(base_img, heat_img)
    draw = ImageDraw.Draw(composite)

    # ── Measurement point dots ────────────────────────────────────────────
    if show_labels:
        _draw_measurement_points(draw, width, height, metric, pts)

    # ── Legend ────────────────────────────────────────────────────────────
    _draw_legend(draw, width, height, metric)

    # ── Encode ────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    composite.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _apply_colormap(norm: "np.ndarray") -> "np.ndarray":
    """Map normalized 0–1 grid to RGBA using a 5-color gradient."""
    import numpy as np
    h, w = norm.shape
    r = np.zeros((h, w), dtype=np.float32)
    g = np.zeros((h, w), dtype=np.float32)
    b = np.zeros((h, w), dtype=np.float32)

    # Blue → Cyan → Green → Yellow → Red
    zones = [
        (0.00, 0.25, (0, 0, 255),   (0, 220, 255)),
        (0.25, 0.50, (0, 220, 255), (0, 200, 0)),
        (0.50, 0.70, (0, 200, 0),   (230, 200, 0)),
        (0.70, 1.00, (230, 200, 0), (255, 30, 0)),
    ]
    for lo, hi, c0, c1 in zones:
        m = (norm >= lo) & (norm < hi)
        t = (norm[m] - lo) / (hi - lo)
        r[m] = c0[0] + t * (c1[0] - c0[0])
        g[m] = c0[1] + t * (c1[1] - c0[1])
        b[m] = c0[2] + t * (c1[2] - c0[2])

    rgba = np.stack([
        np.clip(r, 0, 255).astype(np.uint8),
        np.clip(g, 0, 255).astype(np.uint8),
        np.clip(b, 0, 255).astype(np.uint8),
        np.zeros((h, w), dtype=np.uint8),
    ], axis=-1)
    return rgba


def _draw_measurement_points(draw, width: int, height: int, metric: str,
                             pts: list = None):
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
    except Exception:
        font = None

    for pt in (pts if pts is not None else get_measurements()):
        px = int(pt["x"] * width)
        py = int(pt["y"] * height)
        color = _rssi_color_tuple(pt["rssi"])
        r = 9
        draw.ellipse([px-r, py-r, px+r, py+r], fill=color, outline=(255,255,255,220))

        if metric == "snr":
            label = f'{pt["snr"]} dB'
        elif metric == "quality":
            label = f'{pt["quality_score"]}'
        else:
            label = f'{pt["rssi"]} dBm'

        draw.text((px + r + 2, py - 6), label,
                  fill=(255, 255, 255, 230), font=font)

        # Station count badge if available
        sc = pt.get("station_count")
        if sc is not None:
            draw.text((px - r, py + r + 2), f'↔{sc}',
                      fill=(220, 220, 100, 200), font=font)


def _draw_legend(draw, width: int, height: int, metric: str):
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
    except Exception:
        font = None

    if metric == "snr":
        legend = [
            ("≥ 30 dB 優異",  (34,  197, 94)),
            ("≥ 25 dB 良好",  (132, 204, 22)),
            ("≥ 20 dB 普通",  (234, 179, 8)),
            ("< 20 dB 弱",   (239, 68,  68)),
        ]
        title = "SNR 圖層"
    elif metric == "quality":
        legend = [
            ("≥ 90 優異",  (34,  197, 94)),
            ("≥ 75 良好",  (132, 204, 22)),
            ("≥ 60 普通",  (234, 179, 8)),
            ("< 60 弱",   (239, 68,  68)),
        ]
        title = "品質分數"
    else:
        legend = [
            ("≥ -55 dBm 優異", (34,  197, 94)),
            ("≥ -65 dBm 良好", (132, 204, 22)),
            ("≥ -70 dBm 普通", (234, 179, 8)),
            ("< -70 dBm 死角", (239, 68,  68)),
        ]
        title = "RSSI 圖層"

    bx, by = width - 155, height - len(legend) * 20 - 28
    draw.rectangle([bx-5, by-18, width-5, height-5], fill=(15, 15, 25, 190))
    draw.text((bx, by - 14), title, fill=(200, 200, 200, 220), font=font)
    for i, (label, color) in enumerate(legend):
        y = by + i * 20
        draw.rectangle([bx, y, bx+14, y+12], fill=(*color, 220))
        draw.text((bx + 18, y), label, fill=(220, 220, 220, 255), font=font)


def _fallback_heatmap(width: int, height: int) -> str:
    """PIL/numpy 不可用時的後援（目前回空字串）。"""
    try:
        from PIL import Image
        import numpy as np
        img = Image.new("RGBA", (width, height), (25, 28, 42, 255))
        return ""
    except Exception:
        return ""


# ─── Color helpers ───────────────────────────────────────────────────────
def _rssi_color(rssi: int) -> str:
    if rssi >= -55:  return "#22c55e"
    if rssi >= -65:  return "#84cc16"
    if rssi >= -70:  return "#eab308"
    if rssi >= -75:  return "#f97316"
    return "#ef4444"

def _rssi_color_tuple(rssi: int) -> tuple:
    if rssi >= -55:  return (34,  197, 94,  235)
    if rssi >= -65:  return (132, 204, 22,  235)
    if rssi >= -70:  return (234, 179, 8,   235)
    if rssi >= -75:  return (249, 115, 22,  235)
    return (239, 68, 68, 235)

def _rssi_label(rssi: int) -> str:
    if rssi >= -55:  return "優異"
    if rssi >= -65:  return "良好"
    if rssi >= -70:  return "普通"
    if rssi >= -75:  return "較弱"
    return "信號差"


def export_csv() -> str:
    """NETworkManager-compatible CSV with extended fields."""
    lines = ["ID,X%,Y%,RSSI(dBm),SNR(dB),QualityScore,Label,SSID,StationCount,ChannelUtil%,Timestamp"]
    for p in get_measurements():
        lines.append(
            f"{p['id']},{p['x']:.4f},{p['y']:.4f},{p['rssi']},"
            f"{p['snr']},{p['quality_score']},{p['label']},"
            f"{p['ssid']},{p.get('station_count','')},{p.get('channel_util_pct','')},"
            f"{p['ts']:.0f}"
        )
    return "\n".join(lines)


def export_json() -> str:
    import json
    pts = get_measurements()
    return json.dumps({
        "version":      "2.0",
        "measurements": pts,
        "count":        len(pts),
    }, default=str, ensure_ascii=False, indent=2)
