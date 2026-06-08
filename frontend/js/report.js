/* ═══════════════════════════════════════════════════════════════════════════
   report.js — PDF report generation
   ═══════════════════════════════════════════════════════════════════════════ */

// Set default date to today
document.getElementById("rep-date").valueAsDate = new Date();

document.getElementById("btn-gen-report").addEventListener("click", generateReport);

async function generateReport() {
  const btn    = document.getElementById("btn-gen-report");
  const status = document.getElementById("report-status");
  setLoading(btn, true);
  showStatus(status, "正在產生 PDF 報告，請稍候…");

  try {
    // Gather site info
    const site_info = {
      site:     document.getElementById("rep-site").value || "未填",
      client:   document.getElementById("rep-client").value || "未填",
      surveyor: document.getElementById("rep-surveyor").value || "未填",
      date:     document.getElementById("rep-date").value || new Date().toISOString().slice(0, 10),
    };

    // Ensure we have networks & analysis
    let networks = APP.networks;
    let analysis = APP.analysis;

    if (!networks.length) {
      showStatus(status, "尚無掃描資料，正在自動掃描…");
      const r = await fetch("/api/scan");
      const d = await r.json();
      networks = d.networks || [];
      APP.networks = networks;
    }
    if (!Object.keys(analysis).length) {
      const r = await fetch("/api/analyze", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body:   JSON.stringify({ networks }),
      });
      const d = await r.json();
      analysis = d.analysis || {};
      APP.analysis = analysis;
    }

    // Get heatmap image if available
    let heatmap = "";
    try {
      const r = await fetch("/api/heatmap/generate?w=900&h=600&labels=1");
      const d = await r.json();
      if (d.ok) heatmap = d.image;
    } catch (_) {}

    const resp = await fetch("/api/report", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body:   JSON.stringify({ site_info, networks, analysis, heatmap }),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `wifi_survey_${new Date().toISOString().slice(0,10)}.pdf`;
    a.click();
    URL.revokeObjectURL(url);

    showStatus(status, "PDF 報告已產生並下載！");
  } catch (e) {
    showStatus(status, `報告產生失敗：${e.message}`, true);
  } finally {
    setLoading(btn, false);
  }
}
