/* ═══════════════════════════════════════════════════════════════════════════
   report.js — PDF report generation
   ═══════════════════════════════════════════════════════════════════════════ */

document.getElementById("rep-date").valueAsDate = new Date();
document.getElementById("btn-gen-report").addEventListener("click", generateReport);
document.getElementById("btn-gen-full").addEventListener("click", generateFullReport);

// 整合檢測報告（Wi-Fi + LAN + 診斷，含白標）— 伺服器端彙整
async function generateFullReport() {
  const btn = document.getElementById("btn-gen-full");
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<div class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,.3);border-top-color:#fff"></div> 彙整中…`;
  try {
    const site_info = {
      site:     document.getElementById("rep-site").value,
      client:   document.getElementById("rep-client").value,
      surveyor: document.getElementById("rep-surveyor").value,
      date:     document.getElementById("rep-date").value,
    };
    const r = await fetch("/api/report/full", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site_info }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    a.href = url; a.download = `network-inspection-${ts}.pdf`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    alert("完整報告產生失敗：" + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Capture spectrum canvas as base64 PNG
function _captureSpectrum() {
  const canvas = document.getElementById("spectrum-canvas");
  if (!canvas || !canvas.width) return "";
  try {
    return canvas.toDataURL("image/png");
  } catch (_) { return ""; }
}

async function generateReport() {
  const btn    = document.getElementById("btn-gen-report");
  const status = document.getElementById("report-status");
  setLoading(btn, true);
  showStatus(status, "正在產生 PDF 報告，請稍候…");

  try {
    const site_info = {
      site:     document.getElementById("rep-site").value     || "未填",
      client:   document.getElementById("rep-client").value   || "未填",
      surveyor: document.getElementById("rep-surveyor").value || "未填",
      date:     document.getElementById("rep-date").value     || new Date().toISOString().slice(0, 10),
    };

    // ── Ensure networks ─────────────────────────────────────────────────────
    let networks = APP.networks;
    let analysis = APP.analysis;

    if (!networks.length) {
      showStatus(status, "尚無掃描資料，正在自動掃描…");
      const r = await fetch("/api/scan");
      const d = await r.json();
      networks = d.networks || [];
      APP.networks = networks;
    }

    // ── Ensure analysis ────────────────────────────────────────────────────
    if (!Object.keys(analysis).length) {
      showStatus(status, "正在分析頻道…");
      const r = await fetch("/api/analyze", {
        method:  "POST",
        headers: {"Content-Type": "application/json"},
        body:    JSON.stringify({ networks }),
      });
      const d = await r.json();
      analysis = d.analysis || {};
      APP.analysis = analysis;
    }

    // ── Capture spectrum PNG ───────────────────────────────────────────────
    showStatus(status, "擷取頻譜圖…");
    const spectrum = _captureSpectrum();

    // ── Fetch heatmap if available ────────────────────────────────────────
    let heatmap = "";
    showStatus(status, "載入熱力圖…");
    try {
      const r = await fetch("/api/heatmap/generate?w=1200&h=800&labels=1");
      const d = await r.json();
      if (d.ok) heatmap = d.image;
    } catch (_) {}

    // ── Generate PDF ──────────────────────────────────────────────────────
    showStatus(status, "正在產生 PDF，請稍候…");
    const resp = await fetch("/api/report", {
      method:  "POST",
      headers: {"Content-Type": "application/json"},
      body:    JSON.stringify({ site_info, networks, analysis, heatmap, spectrum }),
    });

    if (!resp.ok) throw new Error(`Server error ${resp.status}`);

    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `wifi_survey_${new Date().toISOString().slice(0,10)}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showStatus(status, "✓ PDF 報告已產生並下載！");
  } catch (e) {
    showStatus(status, `報告產生失敗：${e.message}`, true);
  } finally {
    setLoading(btn, false);
  }
}
