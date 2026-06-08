/* ═══════════════════════════════════════════════════════════════════════════
   fulltest.js — 一鍵完整診斷：執行所有測試 → 顯示優化建議
   ═══════════════════════════════════════════════════════════════════════════ */

document.getElementById("btn-full-test").addEventListener("click", runFullTest);

async function runFullTest() {
  const btn   = document.getElementById("btn-full-test");
  const panel = document.getElementById("diag-panel");
  const prog  = document.getElementById("diag-progress");
  const result= document.getElementById("diag-result");

  // Reset UI
  btn.disabled = true;
  btn.innerHTML = `<div class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,.2);border-top-color:#fff"></div> 診斷中…`;
  panel.classList.remove("hidden");
  result.classList.add("hidden");
  prog.classList.remove("hidden");

  // Show animated progress steps immediately
  const stepLabels = [
    "Wi-Fi 環境掃描",
    "頻道分析",
    "目前連線狀態",
    "網路效能測試（Ping/DNS）",
    "覆蓋量測記錄",
    "智慧分析與建議",
  ];
  showProgressSteps(stepLabels, -1);  // all "running" initially
  showProgressSteps(stepLabels, 0);   // first one running

  try {
    const r = await fetch("/api/full-test", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    "{}",
    });
    const d = await r.json();
    if (!d.ok) throw new Error("診斷失敗");

    // Animate step completions
    for (let i = 0; i < (d.progress || []).length; i++) {
      showProgressSteps(stepLabels, i + 1, d.progress.slice(0, i + 1));
      await sleep(80);
    }

    // Store in app state
    if (d.networks?.length) APP.networks = d.networks;
    if (d.analysis)         APP.analysis = d.analysis;

    // Render results
    prog.classList.add("hidden");
    renderDiagResult(d.assessment, d.current, d.netperf, d.networks);
    result.classList.remove("hidden");

    // Update report preview too
    if (d.analysis?.summary) updateReportPreviewFromDiag(d);

  } catch(e) {
    document.getElementById("diag-steps").innerHTML =
      `<div class="diag-step fail"><span class="step-icon"></span>診斷失敗：${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 12l2 2 4-4"/><path d="M21 12c0 4.97-4.03 9-9 9s-9-4.03-9-9 4.03-9 9-9c1.6 0 3.1.42 4.39 1.15"/><path d="M16 5l2 2 4-4"/></svg> 一鍵完整診斷`;
  }
}

// ── Progress steps renderer ───────────────────────────────────────────────────
function showProgressSteps(labels, currentIdx, completedSteps = []) {
  const el = document.getElementById("diag-steps");
  el.innerHTML = `<div class="diag-steps-grid">` +
    labels.map((label, i) => {
      const done = completedSteps[i];
      if (done) {
        const cls = done.ok ? "ok" : "fail";
        return `<div class="diag-step ${cls}"><span class="step-icon"></span>${label}</div>`;
      }
      if (i === currentIdx) {
        return `<div class="diag-step running"><span class="step-icon"></span>${label}</div>`;
      }
      return `<div class="diag-step"><span class="step-icon" style="opacity:.3">○</span><span style="opacity:.4">${label}</span></div>`;
    }).join("") +
    `</div>`;
}

// ── Render full diagnostic result ─────────────────────────────────────────────
function renderDiagResult(assessment, current, netperf, networks) {
  const { score, grade, issues, counts } = assessment;

  // Score gauge
  drawGauge(score, grade.color);
  document.getElementById("diag-score-num").textContent  = score;
  document.getElementById("diag-score-num").style.color  = grade.color;
  document.getElementById("diag-score-grade").textContent = `${grade.emoji} ${grade.label}`;

  // Count badges
  document.getElementById("diag-counts").innerHTML = [
    { sev:"critical", label:"嚴重", color:"#ef4444" },
    { sev:"warning",  label:"警告", color:"#eab308" },
    { sev:"info",     label:"建議", color:"#3b82f6" },
    { sev:"good",     label:"良好", color:"#22c55e" },
  ].map(({ sev, label, color }) => `
    <div class="diag-count-row">
      <div class="diag-count-dot" style="background:${color}"></div>
      <span class="diag-count-num" style="color:${color}">${counts[sev]}</span>
      <span style="color:var(--text-2)">${label}</span>
    </div>`).join("");

  // Summary text
  const conn = current?.connected
    ? `已連接 ${current.ssid || "—"}（${current.band} ${current.rssi} dBm / SNR ${current.snr} dB）`
    : "未偵測到 Wi-Fi 連線";
  const pingTxt = netperf?.ping_ms != null ? `Ping ${netperf.ping_ms} ms` : "效能測試未完成";
  document.getElementById("diag-summary").innerHTML =
    `<strong style="color:var(--text-1)">診斷摘要</strong><br>
     ${conn}<br>
     偵測 ${networks?.length || 0} 個鄰近網路 ｜ ${pingTxt}<br>
     發現 <span style="color:#ef4444">${counts.critical} 個嚴重問題</span>、
     <span style="color:#eab308">${counts.warning} 個警告</span>、
     <span style="color:#3b82f6">${counts.info} 個優化建議</span>`;

  // Issue list
  const sevOrder   = ["critical","warning","info","good"];
  const sevLabels  = { critical:"🔴 嚴重問題", warning:"🟡 警告", info:"🔵 優化建議", good:"🟢 通過項目" };
  const sevBadge   = { critical:"sev-critical", warning:"sev-warning", info:"sev-info", good:"sev-good" };
  const sevBadgeTx = { critical:"嚴重", warning:"警告", info:"建議", good:"通過" };

  let html = "";
  for (const sev of sevOrder) {
    const group = issues.filter(i => i.severity === sev);
    if (!group.length) continue;
    html += `<div class="diag-section-title">${sevLabels[sev]}（${group.length}）</div>`;
    html += group.map(issue => `
      <div class="diag-issue ${issue.severity}">
        <div class="diag-issue-icon">${issue.icon || "•"}</div>
        <div class="diag-issue-body">
          <div class="diag-issue-title">${issue.title}</div>
          ${issue.problem ? `<div class="diag-issue-problem">${issue.problem}</div>` : ""}
          <div class="diag-issue-rec">${issue.recommendation}</div>
        </div>
        <div>
          <span class="diag-sev-badge ${sevBadge[issue.severity]}">${sevBadgeTx[issue.severity]}</span>
          <div style="font-size:10px;color:var(--text-3);margin-top:3px;text-align:center">${issue.category}</div>
        </div>
      </div>`).join("");
  }
  document.getElementById("diag-issues-list").innerHTML = html;

  // Scroll to panel
  document.getElementById("diag-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ── Score gauge (half-circle) ─────────────────────────────────────────────────
function drawGauge(score, color) {
  const canvas = document.getElementById("diag-gauge");
  if (!canvas) return;
  const ctx  = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H - 8, r = H - 12;

  ctx.clearRect(0, 0, W, H);

  // Background arc
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 2 * Math.PI);
  ctx.strokeStyle = "rgba(255,255,255,.08)";
  ctx.lineWidth   = 10;
  ctx.lineCap     = "round";
  ctx.stroke();

  // Score arc
  const angle = Math.PI + (score / 100) * Math.PI;
  const grad  = ctx.createLinearGradient(0, 0, W, 0);
  grad.addColorStop(0, "#ef4444");
  grad.addColorStop(0.4, "#eab308");
  grad.addColorStop(0.7, "#84cc16");
  grad.addColorStop(1, "#22c55e");

  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, angle);
  ctx.strokeStyle = color;
  ctx.lineWidth   = 10;
  ctx.lineCap     = "round";
  ctx.stroke();

  // Tick marks at 25/50/75
  [25, 50, 75].forEach(pct => {
    const a = Math.PI + (pct / 100) * Math.PI;
    const x1 = cx + (r - 14) * Math.cos(a);
    const y1 = cy + (r - 14) * Math.sin(a);
    const x2 = cx + (r - 7)  * Math.cos(a);
    const y2 = cy + (r - 7)  * Math.sin(a);
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
    ctx.strokeStyle = "rgba(255,255,255,.2)"; ctx.lineWidth = 1; ctx.stroke();
  });
}

// ── Update report preview ─────────────────────────────────────────────────────
function updateReportPreviewFromDiag(d) {
  const el = document.getElementById("rp-content");
  if (!el) return;
  const s = d.analysis?.summary || {};
  const a = d.assessment;
  if (!a) return;
  el.innerHTML = `
    <p><strong>整體健康分數：${a.score}/100 ${a.grade.emoji} ${a.grade.label}</strong></p>
    <p style="margin-top:6px">偵測 ${d.networks?.length||0} 個鄰近網路（2.4G:${(s.bands||{})["2.4GHz"]||0} / 5G:${(s.bands||{})["5GHz"]||0} / 6G:${(s.bands||{})["6GHz"]||0}）</p>
    <p>發現 <span style="color:#ef4444">${a.counts.critical} 個嚴重問題</span>、<span style="color:#eab308">${a.counts.warning} 個警告</span>、<span style="color:#3b82f6">${a.counts.info} 個建議</span></p>
    ${a.issues.filter(i => i.severity === "critical" || i.severity === "warning").slice(0,3).map(i =>
      `<div style="margin-top:8px;padding:6px 10px;background:rgba(255,255,255,.03);border-left:2px solid ${i.severity==="critical"?"#ef4444":"#eab308"};border-radius:3px;font-size:11px">
        <strong>${i.title}</strong><br><span style="color:var(--text-2)">${i.recommendation}</span>
      </div>`).join("")}
  `;
}

// ── Utility ───────────────────────────────────────────────────────────────────
const sleep = ms => new Promise(r => setTimeout(r, ms));
