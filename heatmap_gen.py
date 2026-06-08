"""
Heatmap generator — upgraded to RBF interpolation (scipy) + contour lines
Supports multiple metric layers: RSSI, SNR, quality score
Corner padding prevents boundary artifacts (from python-wifi-survey-heatmap)
Stores full AP scan per measurement point for channel utilization layer
"""
import base64
import io
import time
from typing import Optional

# Session state
_measurements: list[dict] = []
_floorplan_b64: Optional[str] = None
_floorplan_size: tuple[int, int] = (0, 0)


def set_floorplan(b64_data: str) -> dict:
    global _floorplan_b64, _floorplan_size
    try:
        from PIL import Image
        img_bytes = base64.b64decode(b64_data.split(",")[-1])
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
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
        "id":            len(_measurements),
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
    _measurements.append(pt)
    return {"ok": True, "point": pt, "total": len(_measurements)}


def remove_measurement(point_id: int) -> dict:
    global _measurements
    _measurements = [p for p in _measurements if p["id"] != point_id]
    return {"ok": True, "total": len(_measurements)}


def get_measurements() -> list[dict]:
    return list(_measurements)


def clear_measurements() -> dict:
    global _measurements
    _measurements = []
    return {"ok": True}


def generate_heatmap(width: int = 1000, height: int = 700,
                     show_labels: bool = True,
                     metric: str = "rssi",   # "rssi" | "snr" | "quality"
                     show_contours: bool = True) -> str:
    """
    Render multi-metric heatmap on floor plan.
    Uses RBF interpolation with corner padding.
    Returns base64-encoded PNG data URI.
    """
    if len(_measurements) < 2:
        return ""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        from scipy.interpolate import Rbf
    except ImportError:
        return _fallback_heatmap(width, height)

    # ── Extract coordinates and values ───────────────────────────────────
    xs  = np.array([p["x"] * width  for p in _measurements])
    ys  = np.array([p["y"] * height for p in _measurements])

    if metric == "snr":
        vals = np.array([p["snr"]           for p in _measurements], dtype=float)
        vmin, vmax = 0, 50
        contour_vals = [15, 20, 25, 30]
        threshold_colors = {15: "#ef4444", 20: "#f97316", 25: "#eab308", 30: "#22c55e"}
    elif metric == "quality":
        vals = np.array([p["quality_score"] for p in _measurements], dtype=float)
        vmin, vmax = 0, 100
        contour_vals = [40, 60, 75, 90]
        threshold_colors = {40: "#ef4444", 60: "#f97316", 75: "#eab308", 90: "#22c55e"}
    else:  # rssi
        vals = np.array([p["rssi"]          for p in _measurements], dtype=float)
        vmin, vmax = -95, -30
        contour_vals = [-85, -75, -70, -65]
        threshold_colors = {-85: "#ef4444", -75: "#f97316", -70: "#eab308", -65: "#22c55e"}

    # ── Corner padding (prevents boundary artifacts) ─────────────────────
    # Add 4 corner points at min value so RBF extrapolates to "bad signal"
    pad_val = vals.min()
    xs  = np.append(xs,  [0, width, 0,     width])
    ys  = np.append(ys,  [0, 0,    height, height])
    vals = np.append(vals, [pad_val] * 4)

    # ── RBF interpolation (Radial Basis Function — better than IDW) ───────
    try:
        rbf = Rbf(xs, ys, vals, function="linear")
        gx, gy = np.meshgrid(np.linspace(0, width, width // 4),
                              np.linspace(0, height, height // 4))
        grid_z = rbf(gx, gy)
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
    gx2, gy2 = np.meshgrid(np.arange(width), np.arange(height))
    min_dists = np.full((height, width), np.inf)
    for i in range(len(xs_pts)):
        d = np.sqrt((gx2 - xs_pts[i])**2 + (gy2 - ys_pts[i])**2)
        min_dists = np.minimum(min_dists, d)
    falloff = min(width, height) * 0.15
    alpha_f = np.clip(1.0 - (min_dists - falloff) / (falloff * 0.6), 0, 1)
    rgba[:, :, 3] = (alpha_f * 150).astype(np.uint8)

    heat_img = Image.fromarray(rgba, "RGBA")

    # ── Base image ────────────────────────────────────────────────────────
    if _floorplan_b64:
        img_bytes = base64.b64decode(_floorplan_b64.split(",")[-1])
        base_img  = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        base_img  = base_img.resize((width, height), Image.LANCZOS)
    else:
        base_img = Image.new("RGBA", (width, height), (25, 28, 42, 255))

    composite = Image.alpha_composite(base_img, heat_img)
    draw = ImageDraw.Draw(composite)

    # ── Contour lines ─────────────────────────────────────────────────────
    if show_contours and len(_measurements) >= 3:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(1, 1, figsize=(width/100, height/100), dpi=100)
            ax.contour(np.linspace(0, width, width//4),
                       np.linspace(0, height, height//4),
                       grid_z, levels=contour_vals, colors="white",
                       linewidths=0.8, alpha=0.7)
            ax.set_xlim(0, width); ax.set_ylim(0, height)
            ax.axis("off"); fig.patch.set_alpha(0)
            ax.patch.set_alpha(0)
            buf2 = io.BytesIO()
            plt.savefig(buf2, format="png", transparent=True, dpi=100,
                        bbox_inches="tight", pad_inches=0)
            plt.close(fig)
            buf2.seek(0)
            contour_img = Image.open(buf2).convert("RGBA").resize((width, height), Image.LANCZOS)
            composite = Image.alpha_composite(composite, contour_img)
            draw = ImageDraw.Draw(composite)
        except Exception:
            pass

    # ── Measurement point dots ────────────────────────────────────────────
    if show_labels:
        _draw_measurement_points(draw, width, height, metric)

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


def _draw_measurement_points(draw, width: int, height: int, metric: str):
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
    except Exception:
        font = None

    for pt in _measurements:
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
    """Simple PIL-only IDW fallback if scipy unavailable."""
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
    for p in _measurements:
        lines.append(
            f"{p['id']},{p['x']:.4f},{p['y']:.4f},{p['rssi']},"
            f"{p['snr']},{p['quality_score']},{p['label']},"
            f"{p['ssid']},{p.get('station_count','')},{p.get('channel_util_pct','')},"
            f"{p['ts']:.0f}"
        )
    return "\n".join(lines)


def export_json() -> str:
    import json
    return json.dumps({
        "version":      "2.0",
        "measurements": _measurements,
        "count":        len(_measurements),
    }, default=str, ensure_ascii=False, indent=2)
