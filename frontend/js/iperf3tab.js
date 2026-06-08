/* ═══════════════════════════════════════════════════════════════════════════
   iperf3tab.js — Dedicated iperf3 tab
   Server toggle · Real-time SSE log · Live chart · Post-test analysis
   ═══════════════════════════════════════════════════════════════════════════ */

let _ip3Chart   = null;
let _ip3Sse     = null;
let _ip3Mode    = "tcp_dl";
let _srvRunning = false;
let _srvLogPoll = null;

// ── Track live data ───────────────────────────────────────────────────────────
const _live = { mbps: [], retr: 0, startTs: 0, duration: 10 };

// ── Init: check iperf3 and pre-fill IP ───────────────────────────────────────
(async function ip3Init() {
  try {
    const r = await fetch("/api/iperf/check");
    const d = await r.json();
    if (!d.installed) {
      document.getElementById("ip3-install-note").classList.remove("hidden");
      document.getElementById("ip3-run-btn").disabled = true;
      document.getElementById("ip3-srv-btn").disabled = true;
    }
    if (d.local_ips?.length) document.getElementById("ip3-target").value = d.local_ips[0];

    // Restore server state if still running
    const s = await fetch("/api/iperf/server/status").then(r => r.json());
    if (s.running) _applySrvRunning(s);
  } catch (_) {}
})();

// ── Server toggle ─────────────────────────────────────────────────────────────
document.getElementById("ip3-srv-btn").addEventListener("click", async () => {
  const btn = document.getElementById("ip3-srv-btn");
  btn.disabled = true;
  if (_srvRunning) {
    await fetch("/api/iperf/server/stop", { method: "POST" });
    _applySrvStopped();
  } else {
    const port = parseInt(document.getElementById("ip3-srv-port").value) || 5201;
    const r    = await fetch("/api/iperf/server/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port }),
    });
    const d = await r.json();
    if (d.ok) _applySrvRunning(d);
    else alert(`Server 啟動失敗：${d.error}`);
  }
  btn.disabled = false;
});

function _applySrvRunning(data) {
  _srvRunning = true;
  const port = data.port || 5201;
  const ips  = data.ips  || [];
  document.getElementById("ip3-dot").className       = "ip3-dot on";
  document.getElementById("ip3-dot-label").textContent = `運行中 :${port}`;
  document.getElementById("ip3-dot-label").className  = "ip3-dot-label on";
  document.getElementById("ip3-srv-icon").innerHTML   = '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
  document.getElementById("ip3-srv-text").textContent = "停止 Server";
  document.getElementById("ip3-srv-btn").classList.add("stop");

  const cmd = `iperf3 -c ${ips[0] || "?"} -p ${port}`;
  document.getElementById("ip3-ip-row").innerHTML =
    ips.map(ip => `<span class="ip3-ip-chip">${ip}</span>`).join("");
  document.getElementById("ip3-cmd").textContent         = cmd;
  document.getElementById("ip3-cmd").dataset.cmd         = cmd;
  document.getElementById("ip3-srv-info").classList.remove("hidden");
  document.getElementById("ip3-srv-log-wrap").classList.remove("hidden");

  if (ips.length) document.getElementById("ip3-target").value = ips[0];
  document.getElementById("ip3-port").value = port;

  _startSrvLogPoll();
}

function _applySrvStopped() {
  _srvRunning = false;
  if (_srvLogPoll) { clearInterval(_srvLogPoll); _srvLogPoll = null; }
  document.getElementById("ip3-dot").className       = "ip3-dot";
  document.getElementById("ip3-dot-label").textContent = "已停止";
  document.getElementById("ip3-dot-label").className  = "ip3-dot-label";
  document.getElementById("ip3-srv-icon").innerHTML   = '<polygon points="5 3 19 12 5 21 5 3"/>';
  document.getElementById("ip3-srv-text").textContent = "啟動 Server";
  document.getElementById("ip3-srv-btn").classList.remove("stop");
  document.getElementById("ip3-srv-info").classList.add("hidden");
  document.getElementById("ip3-srv-log-wrap").classList.add("hidden");
}

function _startSrvLogPoll() {
  if (_srvLogPoll) clearInterval(_srvLogPoll);
  _srvLogPoll = setInterval(async () => {
    const s = await fetch("/api/iperf/server/status").then(r => r.json()).catch(() => ({}));
    if (!s.running) { _applySrvStopped(); return; }
    const logEl = document.getElementById("ip3-srv-log");
    logEl.innerHTML = (s.log || []).slice(-10).map(l =>
      `<div>${escHtml(l)}</div>`).join("");
    logEl.scrollTop = logEl.scrollHeight;
  }, 2000);
}

window.ip3Copy = () => {
  const cmd = document.getElementById("ip3-cmd").dataset.cmd || "";
  navigator.clipboard?.writeText(cmd).catch(() => {});
  const btn = document.querySelector(".ip3-copy");
  if (btn) { btn.textContent = "✓"; setTimeout(() => btn.textContent = "複製", 1500); }
};

// ── Mode selector ─────────────────────────────────────────────────────────────
document.querySelectorAll(".ip3-mode").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".ip3-mode").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _ip3Mode = btn.dataset.mode;
  });
});

// ── Run test via SSE ──────────────────────────────────────────────────────────
document.getElementById("ip3-run-btn").addEventListener("click", ip3Run);

function ip3Run() {
  if (_ip3Sse) { _ip3Sse.close(); _ip3Sse = null; }

  const target   = document.getElementById("ip3-target").value.trim();
  const port     = parseInt(document.getElementById("ip3-port").value) || 5201;
  const duration = parseInt(document.getElementById("ip3-dur").value)  || 10;
  const streams  = parseInt(document.getElementById("ip3-streams").value) || 1;
  const mode     = _ip3Mode;

  if (!target) { alert("請輸入 Server IP"); return; }

  // Reset UI
  _live.mbps = []; _live.retr = 0;
  _live.startTs = Date.now(); _live.duration = duration;
  _resetLiveUI(duration, mode);
  _clearAnalysis();

  // Build SSE URL
  const params = new URLSearchParams({ target, port, duration, mode, streams });
  const url    = `/api/iperf/stream?${params}`;

  const runBtn = document.getElementById("ip3-run-btn");
  runBtn.disabled = true;
  runBtn.innerHTML = '<div class="spinner" style="width:12px;height:12px;border-color:rgba(255,255,255,.2);border-top-color:#fff"></div> 測試中…';

  _ip3Sse = new EventSource(url);

  _ip3Sse.addEventListener("start", e => {
    const d = JSON.parse(e.data);
    _logLine(`連線到 ${d.target}:${d.port}  模式:${_modeLabel(d.mode)}  時間:${d.duration}s`, "header");
    _logLine(`命令: ${d.cmd}`, "header");
  });

  _ip3Sse.addEventListener("log", e => {
    const d = JSON.parse(e.data);
    const text = d.text || "";
    let cls = "interval";
    if (text.includes("----")) cls = "header";
    else if (text.includes("sender") || text.includes("receiver")) cls = "summary";
    else if (text.includes("error") || text.includes("Error")) cls = "error";
    _logLine(text, cls);
  });

  _ip3Sse.addEventListener("interval", e => {
    const iv = JSON.parse(e.data);
    _live.mbps.push(iv.mbps);
    if (iv.retr != null) _live.retr += iv.retr;
    _updateLiveNums(iv, mode);
    _updateLiveChart(iv);
    _updateProgressBar();
  });

  _ip3Sse.addEventListener("analysis", e => {
    const analysis = JSON.parse(e.data);
    _renderAnalysis(analysis);
  });

  _ip3Sse.addEventListener("error_event", e => {
    const d = JSON.parse(e.data);
    _logLine(`錯誤：${d.message}`, "error");
    _finish(runBtn);
  });

  _ip3Sse.addEventListener("done", () => {
    _finish(runBtn);
    _ip3Sse.close(); _ip3Sse = null;
  });

  _ip3Sse.onerror = () => {
    _finish(runBtn);
    if (_ip3Sse) { _ip3Sse.close(); _ip3Sse = null; }
  };
}

function _finish(runBtn) {
  runBtn.disabled = false;
  runBtn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> 執行測試';
  document.getElementById("ip3-progress-wrap").classList.add("hidden");
  document.getElementById("ip3-progress-fill").style.width = "100%";
}

// ── Live UI helpers ───────────────────────────────────────────────────────────
function _resetLiveUI(duration, mode) {
  // Show sections
  ["ip3-live-area","ip3-progress-wrap","ip3-live-nums","ip3-chart-wrap","ip3-log-section"]
    .forEach(id => document.getElementById(id).classList.remove("hidden"));

  // Reset log
  document.getElementById("ip3-log-box").innerHTML = "";

  // Reset nums
  ["ip3-live-mbps","ip3-live-peak","ip3-live-avg","ip3-live-retr"].forEach(id => {
    document.getElementById(id).textContent = "—";
    document.getElementById(id).style.color = "";
  });

  // Progress bar reset
  document.getElementById("ip3-progress-fill").style.width = "0%";
  document.getElementById("ip3-progress-label").textContent = `0 s / ${duration} s`;

  // Retransmits label
  document.getElementById("ip3-live-retr-wrap").style.display =
    mode === "udp" ? "none" : "";

  // Init chart
  _initLiveChart(mode);
}

function _updateProgressBar() {
  const elapsed = (Date.now() - _live.startTs) / 1000;
  const pct     = Math.min(100, elapsed / _live.duration * 100);
  document.getElementById("ip3-progress-fill").style.width = pct + "%";
  document.getElementById("ip3-progress-label").textContent =
    `${elapsed.toFixed(0)} s / ${_live.duration} s`;
}

function _updateLiveNums(iv, mode) {
  const last = iv.mbps;
  const mbps_arr = _live.mbps;
  const avg  = mbps_arr.length ? mbps_arr.reduce((a,b)=>a+b,0)/mbps_arr.length : 0;
  const peak = mbps_arr.length ? Math.max(...mbps_arr) : 0;
  const color = _speedColor(last);

  document.getElementById("ip3-live-mbps").textContent = `${last.toFixed(1)}`;
  document.getElementById("ip3-live-mbps").style.color = color;
  document.getElementById("ip3-live-peak").textContent = `${peak.toFixed(1)}`;
  document.getElementById("ip3-live-avg").textContent  = `${avg.toFixed(1)}`;

  if (mode !== "udp") {
    document.getElementById("ip3-live-retr").textContent = _live.retr;
    document.getElementById("ip3-live-retr").style.color = _live.retr > 50 ? "#ef4444" : _live.retr > 10 ? "#eab308" : "#22c55e";
  }
}

function _logLine(text, cls = "") {
  if (!text) return;
  const el  = document.getElementById("ip3-log-box");
  const div = document.createElement("div");
  div.className = `ip3-log-line ${cls}`;
  div.textContent = text;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

// ── Live Chart ────────────────────────────────────────────────────────────────
function _initLiveChart(mode) {
  const ctx = document.getElementById("ip3-chart")?.getContext("2d");
  if (!ctx) return;
  if (_ip3Chart) { _ip3Chart.destroy(); _ip3Chart = null; }

  const label = mode === "udp" ? "UDP Mbps" : mode === "tcp_ul" ? "Upload" : "Download";
  _ip3Chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: label + " (Mbps)",
        data: [],
        borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.1)",
        borderWidth: 2, pointRadius: 3, pointBackgroundColor: "#3b82f6",
        tension: 0.3, fill: true,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 200 },
      scales: {
        x: { grid: { color:"rgba(255,255,255,.04)" }, ticks: { color:"#64748b", font:{ size:9 } } },
        y: {
          min: 0,
          grid: { color:"rgba(255,255,255,.05)" },
          ticks: { color:"#64748b", font:{ size:9 } },
          title: { display:true, text:"Mbps", color:"#64748b", font:{ size:9 } },
        }
      },
      plugins: {
        legend: { labels: { color:"#94a3b8", font:{ size:9 }, boxWidth:10 } },
        tooltip: { callbacks: { label: c => ` ${c.parsed.y.toFixed(1)} Mbps` } }
      }
    }
  });
}

function _updateLiveChart(iv) {
  if (!_ip3Chart) return;
  const ds = _ip3Chart.data;
  ds.labels.push(`${iv.t}s`);
  ds.datasets[0].data.push(iv.mbps);

  // Color each point by speed
  const colors = ds.datasets[0].data.map(v => _speedColor(v));
  ds.datasets[0].pointBackgroundColor = colors;
  ds.datasets[0].borderColor = colors[colors.length - 1] || "#3b82f6";

  _ip3Chart.update("active");
}

// ── Analysis render ───────────────────────────────────────────────────────────
function _clearAnalysis() {
  document.getElementById("ip3-analysis").classList.add("hidden");
}

function _renderAnalysis(data) {
  if (!data.ok) {
    document.getElementById("ip3-recs").innerHTML =
      `<div class="ip3-rec critical"><span class="ip3-rec-icon">🔴</span><span class="ip3-rec-text">分析失敗：${data.error}</span></div>`;
    document.getElementById("ip3-analysis").classList.remove("hidden");
    return;
  }

  // ── Score cards ────────────────────────────────────────────────────────────
  const mode = data.mode;
  let cardsHtml = `
    <div class="ip3-score-card">
      <div class="ip3-score-val" style="color:${_speedColor(data.avg_mbps)}">${data.avg_mbps}</div>
      <div class="ip3-score-unit">Mbps 平均</div>
      <div class="ip3-score-label">${data.speed_tier?.label || ""}</div>
    </div>
    <div class="ip3-score-card">
      <div class="ip3-score-val" style="color:${_speedColor(data.peak_mbps)}">${data.peak_mbps}</div>
      <div class="ip3-score-unit">Mbps 峰值</div>
    </div>
    <div class="ip3-score-card">
      <div class="ip3-score-val" style="color:${data.stability_cv > 20 ? "#ef4444" : data.stability_cv > 10 ? "#eab308" : "#22c55e"}">±${data.stability_cv}%</div>
      <div class="ip3-score-unit">速率波動</div>
    </div>`;

  if (mode === "udp") {
    cardsHtml += `
      <div class="ip3-score-card">
        <div class="ip3-score-val" style="color:${data.avg_jitter_ms > 20 ? "#ef4444" : data.avg_jitter_ms > 5 ? "#eab308" : "#22c55e"}">${data.avg_jitter_ms}</div>
        <div class="ip3-score-unit">ms Jitter</div>
      </div>
      <div class="ip3-score-card">
        <div class="ip3-score-val" style="color:${data.avg_lost_pct > 2 ? "#ef4444" : data.avg_lost_pct > 0.5 ? "#eab308" : "#22c55e"}">${data.avg_lost_pct}%</div>
        <div class="ip3-score-unit">封包遺失</div>
      </div>`;
  } else {
    const retr = data.total_retransmits || 0;
    cardsHtml += `
      <div class="ip3-score-card">
        <div class="ip3-score-val" style="color:${retr > 50 ? "#ef4444" : retr > 10 ? "#eab308" : "#22c55e"}">${retr}</div>
        <div class="ip3-score-unit">TCP 重傳次數</div>
      </div>`;
  }
  document.getElementById("ip3-score-cards").innerHTML = cardsHtml;

  // ── Rating bars ────────────────────────────────────────────────────────────
  const ratings = data.ratings || {};
  const ratingRows = [
    { key: "speed",     name: "吞吐量" },
    { key: "stability", name: "穩定性" },
    { key: mode === "udp" ? "udp" : "tcp", name: mode === "udp" ? "UDP 品質" : "TCP 健康度" },
  ];
  document.getElementById("ip3-rating-rows").innerHTML = ratingRows.map(({ key, name }) => {
    const r = ratings[key];
    if (!r) return "";
    return `<div class="ip3-rating-row">
      <span class="ip3-rating-name">${name}</span>
      <div class="ip3-rating-bar-bg">
        <div class="ip3-rating-bar-fill" style="width:${r.score}%;background:${r.color}"></div>
      </div>
      <span class="ip3-rating-tag" style="color:${r.color}">${r.label}</span>
    </div>`;
  }).join("");

  // ── Use cases ──────────────────────────────────────────────────────────────
  const uses = data.use_cases || [];
  document.getElementById("ip3-usecases").innerHTML = uses.length
    ? `<div style="font-size:10px;color:var(--text-3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">適合用途</div>` +
      uses.map(u => `<span class="ip3-usecase-chip">${u}</span>`).join("")
    : "";

  // ── Recommendations ────────────────────────────────────────────────────────
  const recs = data.recommendations || [];
  document.getElementById("ip3-recs").innerHTML = recs.map(r => `
    <div class="ip3-rec ${r.level}">
      <span class="ip3-rec-icon">${r.icon || "•"}</span>
      <span class="ip3-rec-text">${r.text}</span>
    </div>`).join("");

  document.getElementById("ip3-analysis").classList.remove("hidden");
  document.getElementById("ip3-analysis").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _speedColor(mbps) {
  if (!mbps) return "var(--text-3)";
  if (mbps >= 800) return "#22c55e";
  if (mbps >= 200) return "#84cc16";
  if (mbps >= 50)  return "#eab308";
  if (mbps >= 10)  return "#f97316";
  return "#ef4444";
}

function _modeLabel(mode) {
  return { tcp_dl:"TCP 下載", tcp_ul:"TCP 上傳", tcp_bidir:"TCP 雙向", udp:"UDP" }[mode] || mode;
}
