/* ═══════════════════════════════════════════════════════════════════════════
   feature_report.js — 左側每個功能測完都能「匯出報告」(PDF)
   每個分頁只宣告：何時算測完（ready）、報告要放哪些區塊（build）。
   區塊格式（後端 reporter.generate_feature_report 排版）：
     { type:"kv", title, rows:[[k,v]] }            指標
     { type:"table", title, columns, rows, note }  表格
     { type:"image", title, image:dataURL }        圖表截圖
     { type:"text", title, paragraphs:[] }         文字
     { type:"steps", title, items:[{ok,name,detail}] }
     { type:"assessment", title, assessment }      評分與優化建議（Reco 面板的資料）
   ═══════════════════════════════════════════════════════════════════════════ */
const FeatureReport = (() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const txt = el => (el ? el.innerText || el.textContent || "" : "").replace(/\s+/g, " ").trim();
  const now = () => {
    const d = new Date(), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  // ── 共用擷取工具 ──────────────────────────────────────────────────────────
  // 指標卡片：.card-label / .card-val（/ .card-sub）
  function cards(scope) {
    const root = typeof scope === "string" ? document.querySelector(scope) : scope;
    if (!root) return [];
    return [...root.querySelectorAll(".card")].map(c => {
      const k = txt(c.querySelector(".card-label"));
      let v = txt(c.querySelector(".card-val"));
      const sub = txt(c.querySelector(".card-sub"));
      if (sub && sub !== "—") v += `（${sub}）`;
      return [k, v];
    }).filter(([k, v]) => k && v && v !== "—");
  }

  // 畫面上的表格（tbody id）→ columns/rows；略過「動作」欄與空白提示列
  function domTable(tbodyId, dropCols = ["動作"]) {
    const tb = $(tbodyId);
    if (!tb) return null;
    const ths = [...(tb.closest("table")?.querySelectorAll("thead th") || [])].map(txt);
    const keep = ths.map((h, i) => dropCols.includes(h) ? -1 : i).filter(i => i >= 0);
    const rows = [...tb.querySelectorAll("tr")]
      .filter(tr => !tr.classList.contains("empty-row") && tr.children.length > 1)
      .map(tr => keep.map(i => txt(tr.children[i])));
    return { columns: keep.map(i => ths[i]), rows };
  }

  // 沒畫過的 canvas（例如沒資料的頻段）不放進報告。
  // 不能縮圖取樣：細線縮小後會被平均成全透明 → 直接掃原尺寸像素
  function isBlank(cv) {
    try {
      if (typeof Chart !== "undefined" && Chart.getChart(cv)) return false;
      const d = cv.getContext("2d").getImageData(0, 0, cv.width, cv.height).data;
      for (let i = 3; i < d.length; i += 16) if (d[i]) return false;
      return true;
    } catch (_) { return false; }
  }

  // canvas 截圖：Chart.js 是透明底＋淺色字 → 疊在 App 的深色底上，PDF 才看得清楚
  function snap(el, maxW = 1600) {
    if (!el) return "";
    const w = el.naturalWidth || el.width, h = el.naturalHeight || el.height;
    if (!w || !h) return "";
    if (el.tagName === "CANVAS" && isBlank(el)) return "";   // 沒畫過的 canvas 不放進報告
    const k = Math.min(1, maxW / w);
    const c = document.createElement("canvas");
    c.width = Math.round(w * k); c.height = Math.round(h * k);
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#151925";
    ctx.fillRect(0, 0, c.width, c.height);
    try {
      ctx.drawImage(el, 0, 0, c.width, c.height);
      return c.toDataURL("image/png");
    } catch (_) { return ""; }
  }
  const chart = (id, title, caption) => {
    const image = snap($(id));
    return image ? { type: "image", title, image, caption } : null;
  };

  // 注意：Reco、SpectrumTab 等是 const 全域，不在 window 上 → 用 typeof 判斷
  const lastReco = id => (typeof Reco !== "undefined" ? Reco.last(id) : null);

  function reco(id, title) {
    const r = lastReco(id);
    return r ? { type: "assessment", title: title || r.title, assessment: r.assessment } : null;
  }

  const kv = (title, rows) => ({ type: "kv", title, rows: rows.filter(r => r && r[1] != null && r[1] !== "") });
  const fmt = (v, u = "") => (v == null || v === "" ? "—" : `${v}${u}`);

  // ── 各分頁定義 ────────────────────────────────────────────────────────────
  const DEFS = {
    scanner: {
      kind: "scanner", title: "Wi-Fi 掃描報告", subtitle: "Wi-Fi Network Scan Report",
      ready: () => (window.APP?.networks || []).length > 0,
      build() {
        const nets = [...APP.networks].sort((a, b) => (b.rssi ?? -999) - (a.rssi ?? -999));
        const c = APP.current || {};
        const bands = {};
        nets.forEach(n => { bands[n.band] = (bands[n.band] || 0) + 1; });
        const hidden = nets.filter(n => String(n.bssid || "").startsWith("<")).length;
        const secs = [kv("掃描摘要", [
          ["偵測到的網路", `${nets.length} 個`],
          ["頻段分布", Object.entries(bands).map(([b, n]) => `${b} ${n} 個`).join("、")],
          ["開放（未加密）網路", `${nets.filter(n => n.is_open).length} 個`],
          ["目前連線", c.ssid ? `${c.ssid}（${fmt(c.band)} ch${fmt(c.channel)}，${fmt(c.rssi, " dBm")}，SNR ${fmt(c.snr, " dB")}）` : "未連線"],
          hidden ? ["注意", `${hidden} 個網路的名稱與 BSSID 被系統遮蔽（未取得定位授權）`] : null,
        ])];
        const sp = chart("spectrum-canvas", "頻道分布圖");
        if (sp) secs.push(sp);
        secs.push({
          type: "table", title: "網路清單（依訊號強度排序）",
          columns: ["SSID", "BSSID", "廠牌", "頻段", "頻道", "頻寬", "RSSI", "SNR", "安全性", "標準"],
          rows: nets.map(n => [
            String(n.ssid || "").startsWith("<") ? "(隱藏)" : n.ssid, n.bssid, n.vendor || "",
            n.band, n.channel, n.width, fmt(n.rssi, " dBm"), fmt(n.snr, " dB"), n.security,
            String(n.standard || "").replace(/\s*\(.*?\)\s*/g, ""),   // 「Wi-Fi 6 (802.11ax)」→「Wi-Fi 6」，騰出 BSSID 的寬度
          ]),
        });
        secs.push(reco("scan-reco", "鄰近環境優化建議"));
        return secs;
      },
    },

    channels: {
      kind: "channels", title: "頻道分析報告", subtitle: "Channel Analysis Report",
      ready: () => !!window.APP?.analysis?.summary,
      build() {
        const a = APP.analysis || {};
        const secs = [kv("分析摘要", cards("#tab-channels"))];
        [["ch-chart-24", "2.4 GHz 頻道壅塞"], ["ch-chart-5", "5 GHz 頻道壅塞"], ["ch-chart-6", "6 GHz 頻道壅塞"]]
          .forEach(([id, t]) => {
            const band = id.endsWith("24") ? "2.4GHz" : id.endsWith("5") ? "5GHz" : "6GHz";
            if ((a.summary?.bands || {})[band] && window[`_chart_${id}`]) secs.push(chart(id, t));
          });
        const best = [];
        ["2.4GHz", "5GHz", "6GHz"].forEach(b => (a[b]?.best_channels || []).slice(0, 4).forEach((x, i) =>
          best.push([b, `ch${x.channel}${i === 0 ? "（首選）" : ""}`, `${Math.round(x.congestion ?? 0)}/100`,
                     x.occupants ?? 0, x.is_dfs ? "是" : "", x.reason || ""])));
        if (best.length) secs.push({ type: "table", title: "建議頻道",
          columns: ["頻段", "頻道", "壅塞分數", "鄰居數", "DFS", "說明"], rows: best });
        const recs = a.recommendations || [];
        if (recs.length) secs.push({ type: "table", title: "設定建議",
          columns: ["頻段", "建議", "設定值", "說明"], rows: recs.map(r => [r.band, r.action, r.value, r.detail]) });
        return secs;
      },
    },

    spectrumtab: {
      kind: "spectrum", title: "頻譜分析報告", subtitle: "Spectrum Analyzer Report",
      ready: () => {
        const s = typeof SpectrumTab !== "undefined" ? SpectrumTab.reportState?.() : null;
        return !!s && (s.nets.length > 0 || s.sweeps > 0);
      },
      build() {
        const s = SpectrumTab.reportState();
        const secs = [kv("量測摘要", [["頻段", s.band], ["掃描次數 (sweep)", s.sweeps], ...cards("#tab-spectrumtab")])];
        secs.push(chart("spa-live-canvas", `${s.band} 即時頻譜`));
        secs.push(chart("spa-waterfall-canvas", txt($("spa-wf-title")) || "瀑布圖"));
        const nets = s.nets.filter(n => !s.band || n.band === s.band)
          .sort((a, b) => (b.rssi ?? -999) - (a.rssi ?? -999));
        if (nets.length) secs.push({ type: "table", title: `${s.band} 偵測到的 AP`,
          columns: ["SSID", "BSSID", "頻道", "頻寬", "RSSI", "廠牌"],
          rows: nets.map(n => [String(n.ssid || "").startsWith("<") ? "(隱藏)" : n.ssid, n.bssid,
                               n.channel, n.width, fmt(n.rssi, " dBm"), n.vendor || ""]) });
        secs.push(reco("spa-reco", "頻譜優化建議"));
        return secs;
      },
    },

    heatmap: {
      kind: "heatmap", title: "覆蓋熱圖報告", subtitle: "Wi-Fi Coverage Heatmap Report",
      ready: () => parseInt(txt($("meas-count")), 10) > 0,
      async build() {
        const pts = (await (await fetch("/api/heatmap/points")).json()).points || [];
        const metric = $("hm-metric");
        const secs = [];
        const rs = pts.map(p => p.rssi).filter(v => typeof v === "number");
        secs.push(kv("量測摘要", [
          ["量測點數", `${pts.length} 點`],
          rs.length ? ["RSSI 範圍", `${Math.min(...rs)} ～ ${Math.max(...rs)} dBm`] : null,
          rs.length ? ["平均 RSSI", `${Math.round(rs.reduce((a, b) => a + b, 0) / rs.length)} dBm`] : null,
          rs.length ? ["低於 -70 dBm（死角）", `${rs.filter(v => v < -70).length} 點`] : null,
        ]));
        const img = $("heatmap-img");
        const overlayOn = $("heatmap-overlay")?.style.display !== "none" && img?.src;
        const pic = overlayOn ? { type: "image", title: `覆蓋熱圖（${metric?.selectedOptions[0]?.text || "RSSI"}）`, image: snap(img) }
                              : chart("fp-canvas", "平面圖與量測點");
        if (pic && pic.image) secs.push(pic);
        secs.push({ type: "table", title: "量測點明細",
          columns: ["#", "位置 (X, Y)", "SSID", "RSSI", "SNR", "品質分數", "判定"],
          rows: pts.map((p, i) => [i + 1, `${Math.round(p.x * 100)}%, ${Math.round(p.y * 100)}%`,
                                   p.ssid || "", fmt(p.rssi, " dBm"), fmt(p.snr, " dB"), p.quality_score ?? "", p.label || ""]) });
        secs.push(reco("hm-reco", "覆蓋優化建議"));
        return secs;
      },
    },

    monitor: {
      kind: "monitor", title: "即時訊號監控報告", subtitle: "Real-time Signal Monitor Report",
      ready: () => typeof _monSamples !== "undefined" && _monSamples.length > 0,
      build() {
        const samples = typeof _monSamples !== "undefined" ? _monSamples : [];
        const ssid = $("mon-ssid")?.value.trim();
        const secs = [kv("監控摘要", [
          ["監控對象", ssid || (APP.current?.ssid ? `目前連線（${APP.current.ssid}）` : "目前連線")],
          ["樣本數", `${samples.length} 筆`],
          ["取樣間隔", $("mon-interval")?.selectedOptions[0]?.text || ""],
          ...cards("#tab-monitor"),
          ["門檻判定", txt($("mon-thresh-label"))],
        ])];
        secs.push(chart("mon-chart", "RSSI / SNR 時序圖"));
        secs.push(reco("mon-reco", "訊號穩定度優化建議"));
        return secs;
      },
    },

    netperf: {
      kind: "netperf", title: "網路效能測試報告", subtitle: "Network Performance Report",
      ready: () => typeof npHistory !== "undefined" && npHistory.ping.length > 0,
      build() {
        const secs = [kv("測試設定", [
          ["Ping 目標", $("np-host")?.value], ["DNS 伺服器", $("np-dns")?.value],
          ["每輪次數", $("np-samples")?.selectedOptions[0]?.text], ["測試輪數", `${npHistory.ping.length} 輪`],
        ]), kv("最近一次結果", cards("#tab-netperf"))];
        if (npHistory.ping.length > 1) secs.push(chart("np-chart", "Ping / DNS / HTTP 歷史"));
        secs.push({ type: "table", title: "各輪結果",
          columns: ["輪次", "Ping (ms)", "DNS (ms)", "HTTP TTFB (ms)"],
          rows: npHistory.ping.map((v, i) => [`#${i + 1}`, fmt(v), fmt(npHistory.dns[i]), fmt(npHistory.http[i])]) });
        secs.push(reco("np-reco", "網路效能優化建議"));
        return secs;
      },
    },

    iperf: {
      kind: "iperf", title: "iperf3 吞吐量測試報告", subtitle: "iperf3 Throughput Report",
      ready: () => !!window._ip3Last,
      build() {
        const { analysis: d, mode } = window._ip3Last;
        const modeName = { tcp_dl: "TCP 下載", tcp_ul: "TCP 上傳", tcp_bidir: "TCP 雙向", udp: "UDP" }[mode] || mode;
        const rows = [
          ["測試目標", `${$("ip3-target")?.value || "—"}:${$("ip3-port")?.value || 5201}`],
          ["模式", modeName],
          ["平均吞吐量", `${fmt(d.avg_mbps)} Mbps${d.speed_tier?.label ? `（${d.speed_tier.label}）` : ""}`],
          ["峰值", fmt(d.peak_mbps, " Mbps")],
          ["速率波動", d.stability_cv != null ? `±${d.stability_cv}%` : "—"],
        ];
        if (mode === "udp") rows.push(["Jitter", fmt(d.avg_jitter_ms, " ms")], ["封包遺失", fmt(d.avg_lost_pct, "%")]);
        else rows.push(["TCP 重傳次數", fmt(d.total_retransmits ?? 0)]);
        const secs = [kv("測試結果", rows)];
        const rt = d.ratings || {};
        const rr = [["speed", "吞吐量"], ["stability", "穩定性"], [mode === "udp" ? "udp" : "tcp", mode === "udp" ? "UDP 品質" : "TCP 健康度"]]
          .filter(([k]) => rt[k]).map(([k, n]) => [n, `${rt[k].label}（${rt[k].score}/100）`]);
        if (rr.length) secs.push(kv("評等", rr));
        secs.push(chart("ip3-chart", "吞吐量時序圖"));
        if ((d.use_cases || []).length) secs.push({ type: "text", title: "適合用途", paragraphs: [d.use_cases.join("、")] });
        const recs = d.recommendations || [];
        if (recs.length) secs.push({ type: "assessment", title: "分析與建議", assessment: {
          issues: recs.map(r => ({ severity: { critical: "critical", warning: "warning", good: "good" }[r.level] || "info",
                                   title: r.text })) } });
        return secs;
      },
    },

    lan: {
      kind: "lan", title: "設備盤點報告", subtitle: "Network Device Inventory Report",
      ready: () => typeof LAN !== "undefined" && (LAN.getResult()?.hosts || []).length > 0,
      build() {
        const r = LAN.getResult();
        const hosts = r.hosts || [];
        const named = hosts.filter(h => h.name || h.model).length;
        const secs = [kv("盤點摘要", [
          ["網段", r.subnet], ["閘道", r.gateway], ["存活設備", `${hosts.length} 台`],
          ["已辨識名稱 / 型號", `${named} 台`],
          ["類型分布", Object.entries(r.summary || {}).sort((a, b) => b[1] - a[1]).map(([t, c]) => `${t} ${c}`).join("、")],
        ])];
        secs.push({ type: "table", title: "設備清單",
          columns: ["設備名稱", "IP", "MAC", "廠牌", "類型", "型號 / 系統"],
          rows: hosts.map(h => [h.name || "—", h.ip, h.mac || "", h.vendor || "", h.type || "",
                                [h.model, h.os].filter(Boolean).join(" / ") || "—"]) });
        const svc = hosts.filter(h => (h.open_ports || []).length || (h.service_labels || []).length);
        if (svc.length) secs.push({ type: "table", title: "服務與開放埠",
          columns: ["設備", "IP", "宣告的服務", "開放的埠"],
          rows: svc.map(h => [h.name || h.vendor || "—", h.ip, (h.service_labels || []).join("、"),
                              (h.open_ports || []).map((p, i) => `${p}${(h.services || [])[i] ? "/" + h.services[i] : ""}`).join("、")]) });
        secs.push(reco("lan-reco", "設備盤點發現與建議"));
        return secs;
      },
    },

    traffic: {
      kind: "traffic", title: "流量異常監測報告", subtitle: "Traffic Anomaly Report",
      ready: () => !!lastReco("tf-reco") || (domTable("tf-tbody")?.rows.length || 0) > 0,
      build() {
        const secs = [kv("擷取摘要", [
          ["監聽介面", $("tf-iface")?.value], ["擷取時間", $("tf-duration")?.selectedOptions[0]?.text],
          ...cards("#tab-traffic"),
        ])];
        const t = domTable("tf-tbody");
        if (t?.rows.length) secs.push({ type: "table", title: "流量排行 Top Talkers", columns: t.columns, rows: t.rows });
        secs.push(reco("tf-reco", "流量異常分析"));
        return secs;
      },
    },

    diag: {
      kind: "diag", title: "AI 診斷報告", subtitle: "Network Diagnosis Report",
      ready: () => !!lastReco("dg2-reco"),
      build() {
        const r = lastReco("dg2-reco");
        const steps = [...document.querySelectorAll("#dg2-steps-list .dg2-step")].map(s => ({
          ok: s.classList.contains("ok"), name: txt(s.querySelector(".dg2-name")), detail: txt(s.querySelector(".dg2-detail")) }));
        const secs = [];
        if (steps.length && $("dg2-steps")?.style.display !== "none") secs.push({ type: "steps", title: "檢測步驟", items: steps });
        secs.push({ type: "assessment", title: r.title, assessment: r.assessment });
        const ai = $("dg2-ai-out")?.style.display !== "none" ? $("dg2-ai-text")?.textContent : "";
        if (ai) secs.push({ type: "text", title: `AI 深入分析${txt($("dg2-ai-provider"))}`, paragraphs: ai.split(/\n{2,}/) });
        return secs;
      },
    },
  };

  // ── 匯出 ──────────────────────────────────────────────────────────────────
  function siteInfo() {
    return {
      site:     $("rep-site")?.value || "",
      client:   $("rep-client")?.value || "",
      surveyor: $("rep-surveyor")?.value || "",
    };
  }

  async function exportTab(tab, btn) {
    const def = DEFS[tab];
    if (!def) return;
    if (!def.ready()) { alert("請先完成這個功能的測試，再匯出報告。"); return; }
    setLoading(btn, true);
    try {
      const sections = (await def.build()).filter(Boolean);
      const doc = { kind: def.kind, title: def.title, subtitle: def.subtitle, tested_at: now(), sections };
      const ts = now().replace(/[-: ]/g, "").slice(0, 12);
      await downloadURL("/api/report/feature", `NetInspect_${def.kind}_${ts}.pdf`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc, site_info: siteInfo() }),
      });
    } catch (e) {
      alert("報告產生失敗：" + (e.message || e));
    } finally {
      setLoading(btn, false);
      refresh();
    }
  }

  const ICON = '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 18 15 15"/></svg>';
  const buttons = {};

  function mount() {
    for (const tab of Object.keys(DEFS)) {
      const pane = $(`tab-${tab}`);
      if (!pane) continue;
      const b = document.createElement("button");
      b.className = "btn btn-outline fr-btn";
      b.innerHTML = `${ICON}匯出報告`;
      b.addEventListener("click", () => exportTab(tab, b));
      const actions = pane.querySelector(".tab-header .actions");
      if (actions) actions.appendChild(b);
      else {                                       // 掃描頁沒有 tab-header，放在掃描按鈕前
        const scanBtn = $("btn-scan");
        if (!scanBtn) continue;
        b.style.cssText = "font-size: calc(12px * var(--fs, 1));padding:4px 10px";
        scanBtn.parentNode.insertBefore(b, scanBtn);
      }
      buttons[tab] = b;
    }
    refresh();
  }

  // 未測試時按鈕變灰（只檢查目前分頁，成本很低）
  function refresh() {
    for (const [tab, b] of Object.entries(buttons)) {
      if (b.querySelector(".spinner")) continue;
      let ok = false;
      try { ok = DEFS[tab].ready(); } catch (_) {}
      b.disabled = !ok;
      b.title = ok ? "匯出這個功能的測試結果（PDF）" : "請先完成測試";
    }
  }

  document.addEventListener("DOMContentLoaded", mount);
  setInterval(() => { if (!document.hidden) refresh(); }, 1500);
  return { refresh, exportTab, _defs: DEFS };
})();
