/* ═══════════════════════════════════════════════════════════════════════════
   stability.js — 一鍵連線穩定度分析
   POST /api/stability/start → 每秒輪詢 /status 更新圖表/卡片/進度
   → 結束後 GET /result 渲染報告（gauge + 統計 + 發現）→ 可匯出 PDF
   ═══════════════════════════════════════════════════════════════════════════ */

let stabChart  = null;
let _stabPoll  = null;
let _stabResult = null;

document.getElementById("btn-stab-start").addEventListener("click", startStability);
document.getElementById("btn-stab-pdf").addEventListener("click", exportStabilityPdf);

async function startStability() {
  const btn      = document.getElementById("btn-stab-start");
  const duration = parseInt(document.getElementById("stab-duration").value) || 60;
  const status   = document.getElementById("stab-status");

  const r = await fetch("/api/stability/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ duration }),
  });
  const d = await r.json();
  if (!d.ok) { showStatus(status, d.error || "啟動失敗", true); return; }

  _stabResult = null;
  btn.disabled = true;
  btn.innerHTML = `<div class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,.2);border-top-color:#fff"></div> 分析中…`;
  document.getElementById("btn-stab-pdf").classList.add("hidden");
  document.getElementById("stab-result").classList.add("hidden");
  document.getElementById("stab-progress-wrap").classList.remove("hidden");
  status.classList.add("hidden");
  initStabChart();

  _stabPoll = setInterval(pollStability, 1000);
}

async function pollStability() {
  let s;
  try {
    s = await (await fetch("/api/stability/status")).json();
  } catch (_) { return; }

  // Progress
  const pct = Math.min(100, (s.elapsed / s.duration) * 100);
  document.getElementById("stab-progress-fill").style.width = pct + "%";
  document.getElementById("stab-progress-label").textContent =
    `${Math.round(s.elapsed)} s / ${s.duration} s`;

  updateStabChart(s);
  updateStabCards(s);

  if (!s.running) {
    clearInterval(_stabPoll);
    _stabPoll = null;
    if (s.has_result) {
      const d = await (await fetch("/api/stability/result")).json();
      if (d.ok) { _stabResult = d.result; renderStabReport(d.result); }
    }
    const btn = document.getElementById("btn-stab-start");
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="8 12 10.5 14.5 16 9"/></svg> 一鍵分析穩定度`;
    document.getElementById("stab-progress-wrap").classList.add("hidden");
  }
}

// ── Live cards ────────────────────────────────────────────────────────────────
function updateStabCards(s) {
  const last = s.samples[s.samples.length - 1];
  const set  = (id, v) => { document.getElementById(id).innerHTML = v; };

  if (last && last.connected) {
    set("stab-rssi", `${last.rssi} <span style="font-size:11px;color:var(--text-2)">dBm</span>`);
    set("stab-snr",  `${last.snr} <span style="font-size:11px;color:var(--text-2)">dB</span>`);
  } else if (last) {
    set("stab-rssi", `<span style="color:#ef4444">斷線</span>`);
    set("stab-snr", "—");
  }

  const lastMs = arr => {
    for (let i = arr.length - 1; i >= 0; i--)
      if (arr[i].ms !== null) return arr[i].ms;
    return null;
  };
  const gw = lastMs(s.ping_gw), inet = lastMs(s.ping_inet);
  set("stab-gw",   gw   !== null ? `${gw.toFixed(1)} <span style="font-size:11px;color:var(--text-2)">ms</span>` : "—");
  set("stab-inet", inet !== null ? `${inet.toFixed(1)} <span style="font-size:11px;color:var(--text-2)">ms</span>` : "—");

  const all  = [...s.ping_gw, ...s.ping_inet];
  const lost = all.filter(p => p.ms === null).length;
  set("stab-loss", all.length
    ? `${(lost / all.length * 100).toFixed(1)}<span style="font-size:11px;color:var(--text-2)">%</span>`
    : "—");
  set("stab-events", s.events.length
    ? `<span style="color:#eab308">${s.events.length}</span>` : "0");
}

// ── Chart ─────────────────────────────────────────────────────────────────────
function initStabChart() {
  const ctx = document.getElementById("stab-chart").getContext("2d");
  if (stabChart) stabChart.destroy();
  stabChart = new Chart(ctx, {
    type: "line",
    data: { datasets: [
      { label: "RSSI (dBm)", data: [], borderColor: "#3b82f6",
        backgroundColor: "rgba(59,130,246,.08)", fill: true,
        yAxisID: "yRssi", pointRadius: 0, borderWidth: 2, tension: .3 },
      { label: "Ping 閘道器 (ms)", data: [], borderColor: "#22c55e",
        yAxisID: "yMs", pointRadius: 0, borderWidth: 1.5, tension: .25 },
      { label: "Ping 外部 (ms)", data: [], borderColor: "#eab308",
        yAxisID: "yMs", pointRadius: 0, borderWidth: 1.5, tension: .25 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "linear", title: { display: true, text: "秒", color: "#64748b" },
             ticks: { color: "#64748b" }, grid: { color: "rgba(255,255,255,.05)" } },
        yRssi: { position: "left", min: -100, max: -20,
                 title: { display: true, text: "dBm", color: "#3b82f6" },
                 ticks: { color: "#3b82f6" }, grid: { color: "rgba(255,255,255,.05)" } },
        yMs: { position: "right", min: 0,
               title: { display: true, text: "ms", color: "#22c55e" },
               ticks: { color: "#64748b" }, grid: { drawOnChartArea: false } },
      },
      plugins: { legend: { labels: { color: "#94a3b8", boxWidth: 18 } } },
    },
  });
}

function updateStabChart(s) {
  if (!stabChart) return;
  stabChart.data.datasets[0].data =
    s.samples.filter(p => p.connected && p.rssi !== null)
             .map(p => ({ x: p.t, y: p.rssi }));
  stabChart.data.datasets[1].data =
    s.ping_gw.filter(p => p.ms !== null).map(p => ({ x: p.t, y: p.ms }));
  stabChart.data.datasets[2].data =
    s.ping_inet.filter(p => p.ms !== null).map(p => ({ x: p.t, y: p.ms }));
  stabChart.update("none");
}

// ── Report rendering ──────────────────────────────────────────────────────────
function renderStabReport(r) {
  document.getElementById("stab-result").classList.remove("hidden");
  document.getElementById("btn-stab-pdf").classList.remove("hidden");

  drawStabGauge(r.score, r.grade.color);
  document.getElementById("stab-score-num").textContent   = r.score;
  document.getElementById("stab-score-num").style.color   = r.grade.color;
  document.getElementById("stab-score-grade").textContent = `${r.grade.emoji} ${r.grade.label}`;

  // Severity counts
  const counts = { critical: 0, warning: 0, info: 0, good: 0 };
  r.findings.forEach(f => counts[f.severity]++);
  document.getElementById("stab-counts").innerHTML = [
    { sev: "critical", label: "嚴重", color: "#ef4444" },
    { sev: "warning",  label: "警告", color: "#eab308" },
    { sev: "info",     label: "建議", color: "#3b82f6" },
    { sev: "good",     label: "良好", color: "#22c55e" },
  ].map(({ sev, label, color }) => `
    <div class="diag-count-row">
      <div class="diag-count-dot" style="background:${color}"></div>
      <span class="diag-count-num" style="color:${color}">${counts[sev]}</span>
      <span style="color:var(--text-2)">${label}</span>
    </div>`).join("");

  // Summary
  const gw = r.ping_gw, inet = r.ping_inet;
  document.getElementById("stab-summary").innerHTML =
    `<strong style="color:var(--text-1)">穩定度摘要 — ${escHtml(r.ssid)}</strong><br>
     RSSI 平均 ${r.rssi.mean ?? "—"} dBm（波動 ±${r.rssi.std ?? "—"} dB）｜ SNR 平均 ${r.snr.mean ?? "—"} dB<br>
     ${gw && gw.sent ? `閘道器 Ping ${gw.avg_ms} ms（抖動 ${gw.jitter_ms ?? "—"} ms、掉包 ${gw.loss_pct ?? 0}%）` : "未測得閘道器"}<br>
     ${inet && inet.sent ? `外部 Ping ${inet.avg_ms} ms（抖動 ${inet.jitter_ms ?? "—"} ms、掉包 ${inet.loss_pct ?? 0}%）` : ""}<br>
     漫遊 ${r.roam_count} 次 ｜ 斷線 ${r.disconnect_count} 次 ｜ 取樣 ${r.sample_count} 點 / ${r.duration} 秒`;

  // Findings（重用完整診斷的樣式）
  const sevOrder  = ["critical", "warning", "info", "good"];
  const sevLabels = { critical: "🔴 嚴重問題", warning: "🟡 警告", info: "🔵 優化建議", good: "🟢 通過項目" };
  const sevBadge  = { critical: "sev-critical", warning: "sev-warning", info: "sev-info", good: "sev-good" };
  const sevTx     = { critical: "嚴重", warning: "警告", info: "建議", good: "通過" };

  let html = "";
  for (const sev of sevOrder) {
    const group = r.findings.filter(f => f.severity === sev);
    if (!group.length) continue;
    html += `<div class="diag-section-title">${sevLabels[sev]}（${group.length}）</div>`;
    html += group.map(f => `
      <div class="diag-issue ${f.severity}">
        <div class="diag-issue-icon">${f.icon || "•"}</div>
        <div class="diag-issue-body">
          <div class="diag-issue-title">${escHtml(f.title)}</div>
          ${f.detail ? `<div class="diag-issue-problem">${escHtml(f.detail)}</div>` : ""}
          <div class="diag-issue-rec">${escHtml(f.recommendation)}</div>
        </div>
        <div><span class="diag-sev-badge ${sevBadge[f.severity]}">${sevTx[f.severity]}</span></div>
      </div>`).join("");
  }
  document.getElementById("stab-findings-list").innerHTML = html;

  document.getElementById("stab-result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function drawStabGauge(score, color) {
  const canvas = document.getElementById("stab-gauge");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H - 8, rad = H - 12;

  ctx.clearRect(0, 0, W, H);
  ctx.beginPath();
  ctx.arc(cx, cy, rad, Math.PI, 2 * Math.PI);
  ctx.strokeStyle = "rgba(255,255,255,.08)";
  ctx.lineWidth = 10; ctx.lineCap = "round";
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, rad, Math.PI, Math.PI + (score / 100) * Math.PI);
  ctx.strokeStyle = color;
  ctx.lineWidth = 10; ctx.lineCap = "round";
  ctx.stroke();
}

// ── PDF export ────────────────────────────────────────────────────────────────
async function exportStabilityPdf() {
  const btn = document.getElementById("btn-stab-pdf");
  if (!_stabResult) return;
  setLoading(btn, true);
  try {
    const chart = stabChart ? stabChart.toBase64Image("image/png", 1) : "";
    const site_info = {
      site:     document.getElementById("rep-site")?.value || "",
      client:   document.getElementById("rep-client")?.value || "",
      surveyor: document.getElementById("rep-surveyor")?.value || "",
    };
    const r = await fetch("/api/stability/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chart, site_info }),
    });
    if (!r.ok) throw new Error("PDF 產生失敗");
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `wifi_stability_${new Date().toISOString().slice(0,19).replace(/[-T:]/g,"")}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    showStatus(document.getElementById("stab-status"), e.message, true);
  } finally {
    setLoading(btn, false);
  }
}
