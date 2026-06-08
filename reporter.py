"""
Professional PDF report generator — Wi-Fi site survey
Uses reportlab; falls back to HTML if not available
"""
import io
import time
from datetime import datetime


def generate_pdf(site_info: dict, networks: list[dict],
                 analysis: dict, measurements: list[dict],
                 heatmap_b64: str = "") -> bytes:
    """
    Return PDF bytes.
    site_info: {site, client, surveyor, date}
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, Image as RLImage, PageBreak,
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        return _pdf_reportlab(site_info, networks, analysis,
                              measurements, heatmap_b64)
    except ImportError:
        return _pdf_fallback_html(site_info, networks, analysis).encode()


def _pdf_reportlab(site_info, networks, analysis, measurements, heatmap_b64):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, PageBreak,
    )
    from reportlab.lib.enums import TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Heading1"],
                         fontSize=18, spaceAfter=6, textColor=colors.HexColor("#1e40af"))
    H2 = ParagraphStyle("H2", parent=styles["Heading2"],
                         fontSize=13, spaceAfter=4, textColor=colors.HexColor("#1d4ed8"))
    BODY = styles["Normal"]
    SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8)

    story = []

    # ── Cover ────────────────────────────────────────────────────────────────
    story += [
        Spacer(1, 1*cm),
        Paragraph("Wi-Fi 現場勘測報告", H1),
        Paragraph("Wi-Fi Site Survey Report", styles["Heading2"]),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1e40af")),
        Spacer(1, 0.5*cm),
    ]

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cover_data = [
        ["場地 / Site",    site_info.get("site", "—")],
        ["客戶 / Client",  site_info.get("client", "—")],
        ["勘測員 / By",    site_info.get("surveyor", "—")],
        ["日期 / Date",    site_info.get("date", now)],
        ["報告產生",        now],
    ]
    cover_tbl = Table(cover_data, colWidths=[4*cm, 12*cm])
    cover_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#eff6ff")),
        ("FONTSIZE",   (0,0), (-1,-1), 10),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#bfdbfe")),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,0), (-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
    ]))
    story += [cover_tbl, Spacer(1, 1*cm)]

    # ── Summary ──────────────────────────────────────────────────────────────
    summary = analysis.get("summary", {})
    story.append(Paragraph("一、環境摘要", H2))
    sum_data = [
        ["偵測到鄰居網路",   str(summary.get("total_networks", len(networks)))],
        ["2.4 GHz 數量",     str(summary.get("bands", {}).get("2.4GHz", 0))],
        ["5 GHz 數量",       str(summary.get("bands", {}).get("5GHz", 0))],
        ["6 GHz 數量（6E/7）", str(summary.get("bands", {}).get("6GHz", 0))],
        ["同頻干擾熱區",     str(summary.get("co_channel_hotspots", 0)) + " 個頻道"],
        ["偵測到 Wi-Fi 7",   "是" if summary.get("has_wifi7_candidate") else "否"],
    ]
    story += _simple_table(sum_data) + [Spacer(1, 0.5*cm)]

    # ── Network List ─────────────────────────────────────────────────────────
    story.append(Paragraph("二、鄰近網路清單（前 20 筆）", H2))
    net_header = ["SSID", "頻道", "頻段", "頻寬", "RSSI\n(dBm)", "SNR\n(dB)", "安全", "評級"]
    net_data   = [net_header]
    for n in networks[:20]:
        net_data.append([
            n.get("ssid", "")[:24],
            str(n.get("channel", "")),
            n.get("band", ""),
            n.get("width", "") + " MHz",
            str(n.get("rssi", "")),
            str(n.get("snr", "")),
            n.get("security", "")[:10],
            n.get("quality_label", ""),
        ])
    net_tbl = Table(net_data, colWidths=[4*cm,1.5*cm,1.8*cm,1.6*cm,1.4*cm,1.4*cm,2.5*cm,1.5*cm])
    net_tbl.setStyle(_net_table_style())
    story += [net_tbl, Spacer(1, 0.5*cm)]

    # ── Channel Analysis ─────────────────────────────────────────────────────
    story.append(Paragraph("三、頻道分析與建議", H2))
    for band in ("2.4GHz", "5GHz", "6GHz"):
        band_data = analysis.get(band, {})
        best      = band_data.get("best_channels", [])
        if not best:
            continue
        story.append(Paragraph(f"▸ {band}", styles["Heading3"]))
        bch_data = [["建議頻道", "壅塞分數", "鄰居數", "備註"]]
        for b in best[:3]:
            bch_data.append([
                f"ch {b['channel']}",
                f"{b.get('congestion', 0):.0f} / 100",
                str(b.get("occupants", 0)),
                b.get("reason", ""),
            ])
        story += _simple_table(bch_data, header=True) + [Spacer(1, 0.3*cm)]

    # ── Recommendations ──────────────────────────────────────────────────────
    recs = analysis.get("recommendations", [])
    if recs:
        story.append(Paragraph("四、優化建議", H2))
        rec_data = [["頻段", "建議項目", "說明"]]
        for r in recs:
            rec_data.append([r.get("band",""), r.get("action",""), r.get("detail","")])
        story += _simple_table(rec_data, header=True) + [Spacer(1, 0.5*cm)]

    # ── Coverage Measurements ────────────────────────────────────────────────
    if measurements:
        story.append(Paragraph("五、覆蓋量測記錄", H2))
        meas_data  = [["點位", "X%", "Y%", "RSSI(dBm)", "SNR(dB)", "評估"]]
        for m in measurements:
            meas_data.append([
                str(m.get("id","")+1),
                f'{m.get("x",0)*100:.1f}%',
                f'{m.get("y",0)*100:.1f}%',
                str(m.get("rssi","")),
                str(m.get("snr","")),
                m.get("label",""),
            ])
        story += _simple_table(meas_data, header=True) + [Spacer(1, 0.5*cm)]

    # ── Enterprise Standards Reference ───────────────────────────────────────
    story += [PageBreak(), Paragraph("附錄：企業 Wi-Fi 訊號標準（2025-2026）", H2)]
    ref_data = [
        ["指標",                "目標值",          "說明"],
        ["資料區 RSSI",          "≥ -65 dBm",       "一般上網、辦公用途"],
        ["語音/漫遊 RSSI",        "≥ -67 dBm",       "VoIP、視訊會議"],
        ["漫遊重疊區",            "-67 ～ -72 dBm",  "AP 間信號重疊觸發漫遊"],
        ["死角門檻",              "< -70 dBm",       "需補設 AP"],
        ["SNR（訊噪比）",         "≥ 25 dB",         "低於 20 dB 連線品質明顯下降"],
        ["同頻干擾（2.4G）",      "只用 1/6/11",     "全球三個非重疊頻道"],
        ["頻寬（高密度）",         "20/40 MHz",       "降低同頻干擾"],
        ["漫遊協定",              "802.11k/v/r",     "快速漫遊、BSS Transition"],
        ["Wi-Fi 6/6E 6GHz 頻段", "UNII-5/6/7/8",    "干擾最少，最高吞吐"],
    ]
    story += _simple_table(ref_data, header=True)

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _simple_table(data, header=False):
    from reportlab.platypus import Table, TableStyle, Spacer
    from reportlab.lib import colors
    tbl = Table(data)
    style = [
        ("FONTSIZE",    (0,0), (-1,-1), 8),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS", (0,0), (-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING",(0,0), (-1,-1), 4),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
    ]
    if header:
        style += [
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1e40af")),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
            ("FONTSIZE",   (0,0), (-1,0), 8),
        ]
    tbl.setStyle(TableStyle(style))
    return [tbl]


def _net_table_style():
    from reportlab.platypus import TableStyle
    from reportlab.lib import colors
    return TableStyle([
        ("FONTSIZE",     (0,0), (-1,-1), 7),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("GRID",         (0,0), (-1,-1), 0.3, colors.HexColor("#e2e8f0")),
        ("BACKGROUND",   (0,0), (-1,0),  colors.HexColor("#1e40af")),
        ("TEXTCOLOR",    (0,0), (-1,0),  colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
        ("LEFTPADDING",  (0,0), (-1,-1), 3),
        ("RIGHTPADDING", (0,0), (-1,-1), 3),
    ])


def _pdf_fallback_html(site_info, networks, analysis) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = "\n".join(
        f"<tr><td>{n.get('ssid','')}</td><td>{n.get('channel','')}</td>"
        f"<td>{n.get('band','')}</td><td>{n.get('rssi','')} dBm</td>"
        f"<td>{n.get('snr','')} dB</td><td>{n.get('quality_label','')}</td></tr>"
        for n in networks[:30]
    )
    return f"""<!DOCTYPE html><html><head><title>Wi-Fi Site Survey Report</title>
<style>body{{font-family:sans-serif;margin:2cm}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:4px 8px;font-size:12px}}
th{{background:#1e40af;color:white}}</style></head><body>
<h1>Wi-Fi 現場勘測報告</h1>
<p>場地：{site_info.get('site','')}&nbsp;|&nbsp;日期：{now}</p>
<h2>鄰近網路清單</h2>
<table><tr><th>SSID</th><th>頻道</th><th>頻段</th><th>RSSI</th><th>SNR</th><th>評級</th></tr>
{rows}</table></body></html>"""
