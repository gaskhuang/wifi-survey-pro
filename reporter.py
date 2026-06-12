"""
Wi-Fi Survey Pro — PDF report generator v3.0
· CJK font via STHeiti / Hiragino (macOS built-in)
· Sections: cover, summary, spectrum chart, network list,
            channel analysis, heatmap, recommendations, reference
"""
import io
import os
import base64
from datetime import datetime


# ── Font discovery ─────────────────────────────────────────────────────────────
_CJK_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

def _find_cjk_font():
    for p in _CJK_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _register_fonts():
    """Register CJK font with ReportLab. Returns font name to use."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font_path = _find_cjk_font()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("CJK", font_path, subfontIndex=0))
            pdfmetrics.registerFont(TTFont("CJK-Bold", font_path, subfontIndex=0))
            return "CJK"
        except Exception:
            pass
    return "Helvetica"


# ── Public API ─────────────────────────────────────────────────────────────────
def generate_pdf(site_info: dict, networks: list,
                 analysis: dict, measurements: list,
                 heatmap_b64: str = "",
                 spectrum_b64: str = "") -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        return _build_pdf(site_info, networks, analysis,
                          measurements, heatmap_b64, spectrum_b64)
    except ImportError:
        return _fallback_html(site_info, networks, analysis).encode()


# ── Main builder ───────────────────────────────────────────────────────────────
def _build_pdf(site_info, networks, analysis, measurements,
               heatmap_b64, spectrum_b64):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, Image as RLImage, PageBreak, KeepTogether,
    )

    FONT = _register_fonts()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title="Wi-Fi Site Survey Report",
    )

    # ── Styles ──────────────────────────────────────────────────────────────
    BLUE    = colors.HexColor("#1e40af")
    BLUE2   = colors.HexColor("#1d4ed8")
    LBLUE   = colors.HexColor("#eff6ff")
    GRAY    = colors.HexColor("#64748b")
    WHITE   = colors.white

    def st(name, size, bold=False, color=colors.black, align=TA_LEFT, space_after=4):
        return ParagraphStyle(name,
            fontName=FONT, fontSize=size,
            textColor=color, alignment=align,
            spaceAfter=space_after, leading=size * 1.4)

    H1   = st("H1",   18, color=BLUE,  align=TA_CENTER, space_after=8)
    H1s  = st("H1s",  11, color=GRAY,  align=TA_CENTER, space_after=12)
    H2   = st("H2",   13, color=BLUE2, space_after=6)
    H3   = st("H3",   10, color=BLUE2, space_after=4)
    BODY = st("Body",  9, space_after=3)
    SMAL = st("Smal",  8, color=GRAY,  space_after=2)
    CENT = st("Cent",  9, align=TA_CENTER, space_after=2)

    story = []

    # ╔══════════════════════════════════════╗
    # ║  1. COVER                            ║
    # ╚══════════════════════════════════════╝
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    story += [
        Spacer(1, 1.5*cm),
        Paragraph("Wi-Fi 現場勘測報告", H1),
        Paragraph("Wi-Fi Site Survey Report", H1s),
        HRFlowable(width="100%", thickness=2, color=BLUE),
        Spacer(1, 0.6*cm),
    ]

    cover_data = [
        ["場地 / Site",     site_info.get("site",     "—")],
        ["客戶 / Client",   site_info.get("client",   "—")],
        ["勘測員 / By",     site_info.get("surveyor", "—")],
        ["勘測日期 / Date", site_info.get("date",     now)],
        ["報告產生時間",    now],
        ["偵測網路數量",    str(len(networks))],
    ]
    story.append(_kv_table(cover_data, FONT, col_w=[4.5*cm, 12*cm]))
    story.append(Spacer(1, 1*cm))

    # ╔══════════════════════════════════════╗
    # ║  2. SUMMARY CARDS                    ║
    # ╚══════════════════════════════════════╝
    summary = analysis.get("summary", {})
    bands_count = summary.get("bands", {})

    story += [Paragraph("一、環境摘要", H2)]
    sum_data = [
        ["偵測到鄰居網路",       str(summary.get("total_networks", len(networks))) + " 台"],
        ["2.4 GHz 數量",         str(bands_count.get("2.4GHz", 0)) + " 台"],
        ["5 GHz 數量",           str(bands_count.get("5GHz", 0)) + " 台"],
        ["6 GHz / 6E / 7 數量",  str(bands_count.get("6GHz", 0)) + " 台"],
        ["同頻干擾熱區",         str(summary.get("co_channel_hotspots", 0)) + " 個頻道"],
        ["偵測到 Wi-Fi 6E / 7",  "是" if summary.get("has_wifi7_candidate") else "否"],
    ]
    story += [_kv_table(sum_data, FONT), Spacer(1, 0.5*cm)]

    # ╔══════════════════════════════════════╗
    # ║  3. SPECTRUM CHART                   ║
    # ╚══════════════════════════════════════╝
    if spectrum_b64:
        story += [
            Paragraph("二、頻道頻譜圖（Channel Spectrum）", H2),
            Paragraph(
                "下圖為掃描當下各 AP 的訊號強度與頻道佔用分布，bell curve 寬度代表頻寬（20/40/80/160 MHz），"
                "高度代表訊號強度百分比。X 軸為頻道號碼，Y 軸為訊號百分比（0–100%）。",
                BODY),
            Spacer(1, 0.3*cm),
        ]
        try:
            img_bytes = base64.b64decode(spectrum_b64.split(",")[-1])
            img_buf = io.BytesIO(img_bytes)
            img = RLImage(img_buf, width=16*cm, height=6*cm)
            story += [img, Spacer(1, 0.5*cm)]
        except Exception as e:
            story.append(Paragraph(f"（頻譜圖載入失敗：{e}）", SMAL))
    else:
        story.append(Paragraph("二、頻道頻譜圖", H2))
        story.append(Paragraph("（產生報告時未提供頻譜截圖）", SMAL))
        story.append(Spacer(1, 0.3*cm))

    # ╔══════════════════════════════════════╗
    # ║  4. NETWORK LIST                     ║
    # ╚══════════════════════════════════════╝
    story += [PageBreak(), Paragraph("三、鄰近網路清單", H2)]

    net_header = ["#", "SSID", "頻道", "頻段", "頻寬\n(MHz)", "RSSI\n(dBm)", "SNR\n(dB)", "安全", "評級"]
    net_rows = [net_header]
    for i, n in enumerate(networks, 1):
        ssid = n.get("ssid", "—")
        if ssid.startswith("<"):
            ssid = "(隱藏)"
        net_rows.append([
            str(i),
            ssid[:22],
            str(n.get("channel", "")),
            n.get("band", ""),
            str(n.get("width", "")),
            str(n.get("rssi", "")),
            str(n.get("snr", "")),
            n.get("security", "")[:10],
            n.get("quality_label", ""),
        ])
    net_tbl = Table(net_rows,
        colWidths=[0.8*cm, 4.2*cm, 1.2*cm, 1.6*cm, 1.4*cm, 1.4*cm, 1.2*cm, 2.2*cm, 1.4*cm])
    net_tbl.setStyle(_net_style(FONT, BLUE, LBLUE))
    story += [net_tbl, Spacer(1, 0.5*cm)]

    # ╔══════════════════════════════════════╗
    # ║  5. CHANNEL ANALYSIS                 ║
    # ╚══════════════════════════════════════╝
    story += [PageBreak(), Paragraph("四、頻道分析", H2)]

    BAND_DESC = {
        "2.4GHz": "ISM 頻段（2.4 GHz）：全球免執照頻段，非重疊頻道僅有 1、6、11。穿牆能力佳但干擾多，高密度環境不建議主力使用。",
        "5GHz":   "5 GHz 頻段（UNII-1～UNII-4）：頻道數多（25+），可用 80/160 MHz 頻寬，干擾較少，為現代辦公環境主力頻段。",
        "6GHz":   "6 GHz 頻段（Wi-Fi 6E / 7）：全新頻段，幾乎零干擾，支援 80/160/320 MHz，適合高密度、高吞吐場景。",
    }

    for band in ("2.4GHz", "5GHz", "6GHz"):
        band_data = analysis.get(band, {})
        best = band_data.get("best_channels", [])
        if not best and not band_data:
            continue
        story.append(Paragraph(f"▸ {band}", H3))
        story.append(Paragraph(BAND_DESC.get(band, ""), BODY))

        ch_header = ["建議頻道", "壅塞分數", "佔用 AP 數", "說明"]
        ch_rows = [ch_header]
        for b in best[:5]:
            ch_rows.append([
                f"ch {b['channel']}",
                f"{b.get('congestion', 0):.0f} / 100",
                str(b.get("occupants", 0)),
                b.get("reason", "最少干擾頻道"),
            ])
        if len(ch_rows) > 1:
            story.append(_data_table(ch_rows, FONT, BLUE, header=True))
        story.append(Spacer(1, 0.4*cm))

    # ╔══════════════════════════════════════╗
    # ║  6. HEATMAP                          ║
    # ╚══════════════════════════════════════╝
    if heatmap_b64:
        story += [
            Paragraph("五、訊號熱力圖（Signal Heatmap）", H2),
            Paragraph(
                "熱力圖以色彩呈現各量測點的訊號強度分布。綠色 ≥ -65 dBm（優），"
                "黃色 -65～-72 dBm（尚可），紅色 < -72 dBm（弱，建議補設 AP）。",
                BODY),
            Spacer(1, 0.3*cm),
        ]
        try:
            img_bytes = base64.b64decode(heatmap_b64.split(",")[-1])
            img_buf = io.BytesIO(img_bytes)
            img = RLImage(img_buf, width=14*cm, height=9*cm)
            story += [img, Spacer(1, 0.5*cm)]
        except Exception as e:
            story.append(Paragraph(f"（熱力圖載入失敗：{e}）", SMAL))

    # ╔══════════════════════════════════════╗
    # ║  7. COVERAGE MEASUREMENTS            ║
    # ╚══════════════════════════════════════╝
    if measurements:
        sec_num = "六" if heatmap_b64 else "五"
        story += [Paragraph(f"{sec_num}、覆蓋量測記錄", H2)]
        meas_header = ["量測點", "X%", "Y%", "RSSI (dBm)", "SNR (dB)", "評估"]
        meas_rows = [meas_header]
        for m in measurements:
            meas_rows.append([
                str(m.get("id", 0) + 1),
                f'{m.get("x", 0)*100:.1f}%',
                f'{m.get("y", 0)*100:.1f}%',
                str(m.get("rssi", "—")),
                str(m.get("snr", "—")),
                m.get("label", "—"),
            ])
        story += [_data_table(meas_rows, FONT, BLUE, header=True),
                  Spacer(1, 0.5*cm)]

    # ╔══════════════════════════════════════╗
    # ║  8. RECOMMENDATIONS                  ║
    # ╚══════════════════════════════════════╝
    recs = analysis.get("recommendations", [])
    if recs:
        story += [PageBreak(), Paragraph("七、優化建議", H2),
                  Paragraph(
                      "以下建議根據現場掃描結果自動產生，涵蓋頻道選擇、頻段升級、安全設定等面向。"
                      "請依照場地實際條件評估後執行。", BODY),
                  Spacer(1, 0.3*cm)]

        # Priority color mapping
        PRIORITY_COLOR = {
            "high":   colors.HexColor("#fef2f2"),
            "medium": colors.HexColor("#fffbeb"),
            "low":    colors.HexColor("#f0fdf4"),
        }

        for i, r in enumerate(recs, 1):
            priority = r.get("priority", "medium")
            bg = PRIORITY_COLOR.get(priority, colors.white)
            badge = {"high": "🔴 高優先", "medium": "🟡 中優先", "low": "🟢 低優先"}.get(priority, "")

            rec_block = [
                [f"建議 {i}：{r.get('action', '')}",
                 Paragraph(badge, SMAL)],
            ]
            rec_tbl = Table(rec_block, colWidths=[13*cm, 3*cm])
            rec_tbl.setStyle(TableStyle([
                ("BACKGROUND",  (0,0), (-1,-1), bg),
                ("FONTNAME",    (0,0), (-1,-1), FONT),
                ("FONTSIZE",    (0,0), (-1,-1), 9),
                ("TOPPADDING",  (0,0), (-1,-1), 5),
                ("BOTTOMPADDING",(0,0),(-1,-1), 5),
                ("LEFTPADDING", (0,0), (-1,-1), 6),
                ("BOX",         (0,0), (-1,-1), 0.5, BLUE2),
            ]))

            detail_rows = []
            if r.get("band"):
                detail_rows.append(["頻段", r["band"]])
            if r.get("detail"):
                detail_rows.append(["說明", r["detail"]])
            if r.get("current_channel"):
                detail_rows.append(["目前頻道", f"ch {r['current_channel']}"])
            if r.get("recommended_channel"):
                detail_rows.append(["建議切換至", f"ch {r['recommended_channel']}"])
            if r.get("expected_improvement"):
                detail_rows.append(["預期改善", r["expected_improvement"]])

            elements = [rec_tbl]
            if detail_rows:
                elements.append(_kv_table(detail_rows, FONT,
                                          col_w=[3*cm, 13*cm], font_size=8))
            elements.append(Spacer(1, 0.35*cm))
            story.append(KeepTogether(elements))

    # ╔══════════════════════════════════════╗
    # ║  9. ENTERPRISE REFERENCE             ║
    # ╚══════════════════════════════════════╝
    story += [PageBreak(), Paragraph("附錄：企業 Wi-Fi 設計標準（2025–2026）", H2),
              Paragraph(
                  "以下為業界通用的 Wi-Fi 設計標準，供規劃 AP 佈點、頻道規劃及安全設定之參考依據。",
                  BODY),
              Spacer(1, 0.3*cm)]

    ref_rows = [
        ["指標",               "目標值",         "說明"],
        ["資料區 RSSI",         "≥ -65 dBm",      "一般上網、辦公用途最低要求"],
        ["語音 / 視訊 RSSI",   "≥ -67 dBm",      "VoIP、Teams、Zoom 等即時通訊"],
        ["漫遊觸發區間",        "-67 ～ -72 dBm", "AP 間訊號重疊，觸發 802.11r 快速漫遊"],
        ["死角判定門檻",        "< -70 dBm",      "低於此值需補設 AP 或調整天線方向"],
        ["SNR（訊噪比）",       "≥ 25 dB",        "低於 20 dB 連線品質明顯下降，< 10 dB 斷線"],
        ["2.4G 非重疊頻道",    "1 / 6 / 11",     "全球標準三頻道，避免使用 2~5、7~10"],
        ["5G 高密度頻寬",       "20 / 40 MHz",    "降低同頻干擾；低密度環境可用 80 MHz"],
        ["漫遊協定",            "802.11k/v/r",    "k=鄰居報告、v=BSS Transition、r=快速漫遊"],
        ["安全設定（新部署）",  "WPA3-SAE",       "或 WPA2/3 混合；禁用 WEP / TKIP"],
        ["6GHz 頻段（6E/7）",  "UNII-5 ～ UNII-8","干擾極少，支援 160/320 MHz，高吞吐首選"],
        ["AP 密度（辦公）",     "20 ～ 30 台/樓",  "依隔間、障礙物調整，建議現場勘測確認"],
    ]
    story.append(_data_table(ref_rows, FONT, BLUE, header=True))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ── Stability report ───────────────────────────────────────────────────────────
def generate_stability_pdf(result: dict, chart_b64: str = "",
                           site_info: dict | None = None) -> bytes:
    """連線穩定度分析 PDF：評分、統計、事件、發現與建議、時序圖。"""
    try:
        from reportlab.lib.pagesizes import A4  # noqa: F401 — probe availability
        return _build_stability_pdf(result, chart_b64, site_info or {})
    except ImportError:
        return _fallback_stability_html(result).encode()


def _build_stability_pdf(result, chart_b64, site_info):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
        Image as RLImage,
    )

    FONT = _register_fonts()
    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
        title="Wi-Fi Connection Stability Report")

    BLUE  = colors.HexColor("#1e40af")
    BLUE2 = colors.HexColor("#1d4ed8")
    GRAY  = colors.HexColor("#64748b")

    def st(name, size, color=colors.black, align=TA_LEFT, space_after=4):
        return ParagraphStyle(name, fontName=FONT, fontSize=size,
                              textColor=color, alignment=align,
                              spaceAfter=space_after, leading=size*1.4)

    H1   = st("H1", 18, color=BLUE,  align=TA_CENTER, space_after=8)
    H1s  = st("H1s", 11, color=GRAY, align=TA_CENTER, space_after=12)
    H2   = st("H2", 13, color=BLUE2, space_after=6)
    BODY = st("Body", 9, space_after=3)
    SMAL = st("Smal", 8, color=GRAY, space_after=2)

    grade = result.get("grade", {})
    score = result.get("score", 0)
    gcol  = colors.HexColor(grade.get("color", "#64748b"))
    BIG   = st("Big", 26, color=gcol, align=TA_CENTER, space_after=2)
    GLAB  = st("Glab", 13, color=gcol, align=TA_CENTER, space_after=12)

    story = [
        Paragraph("Wi-Fi 連線穩定度報告", H1),
        Paragraph("Wi-Fi Connection Stability Report", H1s),
        HRFlowable(width="100%", thickness=2, color=BLUE),
        Spacer(1, 0.5*cm),
    ]

    # ── 受測連線 ──
    info_rows = [
        ["SSID",     str(result.get("ssid", "—"))],
        ["BSSID",    str(result.get("bssid", "—"))],
        ["頻道",     (f"{result.get('band') or ''} ch{result.get('channel')}".strip()
                      if result.get("channel") else "—")],
        ["標準",     str(result.get("standard") or "—")],
        ["測試時間", f"{result.get('tested_at', '—')}（{result.get('duration', 0)} 秒）"],
        ["取樣數",   f"{result.get('sample_count', 0)} 點"],
    ]
    if site_info.get("site"):
        info_rows.insert(0, ["場地", site_info["site"]])
    story += [Paragraph("一、受測連線", H2),
              _kv_table(info_rows, FONT), Spacer(1, 0.5*cm)]

    # ── 總評 ──
    story += [
        Paragraph("二、穩定度總評", H2),
        Paragraph(f"{score} / 100", BIG),
        Paragraph(f"{grade.get('emoji','')} {grade.get('label','—')}", GLAB),
    ]

    # ── 統計 ──
    rssi, snr, tx = result.get("rssi", {}), result.get("snr", {}), result.get("tx", {})
    def fmt(v, unit=""):
        return f"{v}{unit}" if v is not None else "—"
    stat_rows = [["指標", "平均", "最小", "最大", "波動（標準差/抖動）"]]
    stat_rows.append(["RSSI (dBm)", fmt(rssi.get("mean")), fmt(rssi.get("min")),
                      fmt(rssi.get("max")), fmt(rssi.get("std"))])
    stat_rows.append(["SNR (dB)", fmt(snr.get("mean")), fmt(snr.get("min")),
                      fmt(snr.get("max")), fmt(snr.get("std"))])
    stat_rows.append(["Tx Rate (Mbps)", fmt(tx.get("mean")), fmt(tx.get("min")),
                      fmt(tx.get("max")), fmt(tx.get("std"))])
    for key, label in (("ping_gw", "Ping 閘道器"), ("ping_inet", "Ping 外部")):
        p = result.get(key)
        if p and p.get("sent"):
            stat_rows.append([
                f"{label} {p.get('host','')} (ms)",
                fmt(p.get("avg_ms")), fmt(p.get("min_ms")), fmt(p.get("max_ms")),
                f"抖動 {fmt(p.get('jitter_ms'))} / 掉包 {fmt(p.get('loss_pct'),'%')}",
            ])
    story += [Paragraph("三、量測統計", H2),
              _data_table(stat_rows, FONT, BLUE, header=True),
              Spacer(1, 0.5*cm)]

    # ── 時序圖 ──
    if chart_b64:
        story.append(Paragraph("四、訊號與延遲時序圖", H2))
        try:
            img_buf = io.BytesIO(base64.b64decode(chart_b64.split(",")[-1]))
            story += [RLImage(img_buf, width=16*cm, height=7*cm),
                      Spacer(1, 0.5*cm)]
        except Exception as e:
            story.append(Paragraph(f"（圖表載入失敗：{e}）", SMAL))

    # ── 事件 ──
    events = result.get("events", [])
    sec = "五" if chart_b64 else "四"
    story.append(Paragraph(f"{sec}、事件記錄", H2))
    if events:
        ev_rows = [["時間 (秒)", "類型", "說明"]]
        type_label = {"roam": "漫遊", "disconnect": "斷線",
                      "reconnect": "重連", "channel_change": "頻道切換"}
        for e in events:
            ev_rows.append([f"{e['t']:.0f}",
                            type_label.get(e["type"], e["type"]), e["detail"]])
        story.append(_data_table(ev_rows, FONT, BLUE, header=True))
    else:
        story.append(Paragraph("測試期間無漫遊、斷線或頻道切換事件。", BODY))
    story.append(Spacer(1, 0.5*cm))

    # ── 發現與建議 ──
    sec2 = "六" if chart_b64 else "五"
    story.append(Paragraph(f"{sec2}、發現與建議", H2))
    SEV_LABEL = {"critical": "【嚴重】", "warning": "【警告】",
                 "info": "【建議】", "good": "【良好】"}
    SEV_COLOR = {"critical": "#dc2626", "warning": "#ca8a04",
                 "info": "#2563eb", "good": "#16a34a"}
    for f in result.get("findings", []):
        c = SEV_COLOR.get(f["severity"], "#000000")
        story.append(Paragraph(
            f'<font color="{c}">{SEV_LABEL.get(f["severity"], "")}'
            f'{f["title"]}</font>', BODY))
        if f.get("detail"):
            story.append(Paragraph(f'　{f["detail"]}', SMAL))
        story.append(Paragraph(f'　→ {f["recommendation"]}', SMAL))
        story.append(Spacer(1, 0.15*cm))

    story += [
        Spacer(1, 0.6*cm),
        HRFlowable(width="100%", thickness=0.8, color=GRAY),
        Paragraph(f"由 Wi-Fi Survey Pro 產生 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                  st("Foot", 8, color=GRAY, align=TA_CENTER)),
    ]

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _fallback_stability_html(result) -> str:
    grade = result.get("grade", {})
    rows = "\n".join(
        f"<li>[{f['severity']}] {f['title']} — {f['recommendation']}</li>"
        for f in result.get("findings", []))
    return f"""<html><meta charset="utf-8"><body>
<h1>Wi-Fi 連線穩定度報告</h1>
<p>SSID: {result.get('ssid')} ｜ 分數: {result.get('score')}/100
（{grade.get('label','')}）</p><ul>{rows}</ul></body></html>"""


# ── Table helpers ──────────────────────────────────────────────────────────────
def _kv_table(data, font, col_w=None, font_size=9):
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    if col_w is None:
        col_w = [4*cm, 12*cm]
    tbl = Table(data, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("FONTNAME",     (0,0), (-1,-1), font),
        ("FONTSIZE",     (0,0), (-1,-1), font_size),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("GRID",         (0,0), (-1,-1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND",   (0,0), (0,-1),  colors.HexColor("#eff6ff")),
        ("ROWBACKGROUNDS",(0,0),(-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ]))
    return tbl


def _data_table(data, font, header_color, header=False):
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    tbl = Table(data)
    style = [
        ("FONTNAME",      (0,0), (-1,-1), font),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.4, colors.HexColor("#e2e8f0")),
        ("ROWBACKGROUNDS",(0,1), (-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
        ("RIGHTPADDING",  (0,0), (-1,-1), 5),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]
    if header:
        style += [
            ("BACKGROUND", (0,0), (-1,0), header_color),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
            ("FONTNAME",   (0,0), (-1,0), font),
            ("FONTSIZE",   (0,0), (-1,0), 8),
        ]
    tbl.setStyle(TableStyle(style))
    return tbl


def _net_style(font, header_color, alt_color):
    from reportlab.platypus import TableStyle
    from reportlab.lib import colors
    return TableStyle([
        ("FONTNAME",      (0,0), (-1,-1), font),
        ("FONTSIZE",      (0,0), (-1,-1), 7),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#e2e8f0")),
        ("BACKGROUND",    (0,0), (-1,0),  header_color),
        ("TEXTCOLOR",     (0,0), (-1,0),  colors.white),
        ("ROWBACKGROUNDS",(0,1), (-1,-1),
         [colors.HexColor("#f8fafc"), colors.white]),
        ("LEFTPADDING",   (0,0), (-1,-1), 3),
        ("RIGHTPADDING",  (0,0), (-1,-1), 3),
        ("TOPPADDING",    (0,0), (-1,-1), 2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
    ])


# ── HTML fallback ──────────────────────────────────────────────────────────────
def _fallback_html(site_info, networks, analysis) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = "\n".join(
        f"<tr><td>{n.get('ssid','—')}</td><td>{n.get('channel','')}</td>"
        f"<td>{n.get('band','')}</td><td>{n.get('rssi','')} dBm</td>"
        f"<td>{n.get('snr','')} dB</td><td>{n.get('quality_label','')}</td></tr>"
        for n in networks
    )
    return f"""<!DOCTYPE html><html><head><title>Wi-Fi Survey Report</title>
<meta charset="utf-8">
<style>body{{font-family:sans-serif;margin:2cm}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:4px 8px;font-size:12px}}
th{{background:#1e40af;color:white}}</style></head><body>
<h1>Wi-Fi 現場勘測報告</h1>
<p>場地：{site_info.get('site','')} | 日期：{now}</p>
<h2>鄰近網路清單</h2>
<table><tr><th>SSID</th><th>頻道</th><th>頻段</th>
<th>RSSI</th><th>SNR</th><th>評級</th></tr>
{rows}</table></body></html>"""
