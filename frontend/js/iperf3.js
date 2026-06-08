/* ═══════════════════════════════════════════════════════════════════════════
   iperf3.js — Server toggle + client test + throughput chart
   ═══════════════════════════════════════════════════════════════════════════ */

let iperfChart = null;
let _serverRunning = false;
let _logPoller = null;
let _selectedMode = "tcp_dl";

// ── Init: check if iperf3 is installed and pre-fill local IP ─────────────────
(async function init() {
  try {
    const r = await fetch("/api/iperf/check");
    const d = await r.json();
    if (!d.installed) {
      document.getElementById("iperf-toggle-label").textContent = "iperf3 未安裝";
      document.getElementById("btn-iperf-toggle").disabled = true;
      document.getElementById("btn-iperf-toggle").title = "請執行: brew install iperf3";
    }
    // Pre-fill server IP into client target
    const ips = d.local_ips || [];
    if (ips.length) {
      document.getElementById("iperf-target").value = ips[0];
    }
    // Check if server already running (e.g. from a previous session)
    const s = await fetch("/api/iperf/server/status").then(r => r.json());
    if (s.running) _applyServerRunning(s);
  } catch (_) {}
})();

// ── Server toggle button ──────────────────────────────────────────────────────
document.getElementById("btn-iperf-toggle").addEventListener("click", async () => {
  const btn = document.getElementById("btn-iperf-toggle");
  btn.disabled = true;

  if (_serverRunning) {
    // Stop
    await fetch("/api/iperf/server/stop", { method: "POST" });
    _applyServerStopped();
  } else {
    // Start
    const port = parseInt(document.getElementById("iperf-port").value) || 5201;
    const r    = await fetch("/api/iperf/server/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ port }),
    });
    const d = await r.json();
    if (d.ok) {
      _applyServerRunning(d);
    } else {
      alert(`啟動失敗：${d.error}`);
    }
  }
  btn.disabled = false;
});

function _applyServerRunning(data) {
  _serverRunning = true;
  const port = data.port || 5201;
  const ips  = data.ips  || [];

  // Status indicator
  document.getElementById("iperf-srv-dot").className   = "iperf-status-dot running";
  document.getElementById("iperf-srv-label").textContent = `運行中 :${port}`;
  document.getElementById("iperf-srv-label").className   = "iperf-status-label running";

  // Toggle button → Stop
  const btn = document.getElementById("btn-iperf-toggle");
  document.getElementById("iperf-toggle-icon").innerHTML =
    '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
  document.getElementById("iperf-toggle-label").textContent = "停止 Server";
  btn.classList.add("stop");

  // Show IP + command
  const ipHtml = ips.map(ip => `<span class="iperf-ip-chip">${ip}</span>`).join("");
  document.getElementById("iperf-ip-list").innerHTML = ipHtml;
  const cmd = `iperf3 -c ${ips[0] || "?"} -p ${port}`;
  document.getElementById("iperf-cmd-display").textContent = cmd;
  document.getElementById("iperf-cmd-display").dataset.cmd = cmd;
  document.getElementById("iperf-srv-info").classList.remove("hidden");

  // Replace QR placeholder with connection tips
  document.getElementById("iperf-qr-panel").innerHTML = `
    <div style="font-size:10px;color:var(--text-3);text-align:center;line-height:1.8">
      <div style="color:#93c5fd;font-weight:700;margin-bottom:4px">從其他設備連線：</div>
      <div>Windows:<br><code style="font-size:9px">iperf3 -c ${ips[0]||"?"}</code></div>
      <div style="margin-top:6px">Android:<br>安裝 iPerf App<br>填入 ${ips[0]||"?"}</div>
    </div>`;

  // Start log polling
  _startLogPoller();

  // Auto-fill client target
  if (ips.length) document.getElementById("iperf-target").value = ips[0];
  document.getElementById("iperf-client-port").value = port;
}

function _applyServerStopped() {
  _serverRunning = false;
  if (_logPoller) { clearInterval(_logPoller); _logPoller = null; }

  document.getElementById("iperf-srv-dot").className    = "iperf-status-dot";
  document.getElementById("iperf-srv-label").textContent = "已停止";
  document.getElementById("iperf-srv-label").className   = "iperf-status-label";

  const btn = document.getElementById("btn-iperf-toggle");
  document.getElementById("iperf-toggle-icon").innerHTML =
    '<polygon points="5 3 19 12 5 21 5 3"/>';
  document.getElementById("iperf-toggle-label").textContent = "啟動 Server";
  btn.classList.remove("stop");
  document.getElementById("iperf-srv-info").classList.add("hidden");

  document.getElementById("iperf-qr-panel").innerHTML = `
    <div class="iperf-qr-hint">
      <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="rgba(255,255,255,.2)" stroke-width="1.5"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
      <span style="font-size:10px;color:var(--text-3)">啟動 Server 後顯示連線資訊</span>
    </div>`;
}

function _startLogPoller() {
  if (_logPoller) clearInterval(_logPoller);
  _logPoller = setInterval(async () => {
    try {
      const s = await fetch("/api/iperf/server/status").then(r => r.json());
      if (!s.running) { _applyServerStopped(); return; }
      const logEl = document.getElementById("iperf-log");
      logEl.innerHTML = (s.log || []).slice(-8).map(l =>
        `<div>${escHtml(l)}</div>`).join("");
      logEl.scrollTop = logEl.scrollHeight;
    } catch (_) {}
  }, 2000);
}

// ── Copy command ───────────────────────────────────────────────────────────────
window.copyIperfCmd = () => {
  const cmd = document.getElementById("iperf-cmd-display").dataset.cmd || "";
  navigator.clipboard?.writeText(cmd).catch(() => {});
  const btn = document.querySelector(".iperf-copy-btn");
  if (btn) { btn.textContent = "✓ 已複製"; setTimeout(() => { btn.textContent = "複製"; }, 1500); }
};

// ── Mode selector ─────────────────────────────────────────────────────────────
document.querySelectorAll(".iperf-mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".iperf-mode-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    _selectedMode = btn.dataset.mode;
  });
});

// ── Run client test ────────────────────────────────────────────────────────────
document.getElementById("btn-iperf-run").addEventListener("click", runIperfTest);

async function runIperfTest() {
  const btn      = document.getElementById("btn-iperf-run");
  const target   = document.getElementById("iperf-target").value.trim();
  const port     = parseInt(document.getElementById("iperf-client-port").value) || 5201;
  const duration = parseInt(document.getElementById("iperf-duration").value)    || 10;
  const streams  = parseInt(document.getElementById("iperf-streams").value)     || 1;
  const mode     = _selectedMode;

  if (!target) { alert("請輸入 Server IP"); return; }

  setLoading(btn, true);
  showStatus(document.getElementById("np-status"),
    `iperf3 ${_modeLabel(mode)} 測試中（${target}:${port} / ${duration}s / ${streams} 串流）…`);

  // Reset result UI
  document.getElementById("iperf-result-cards").classList.add("hidden");
  document.getElementById("iperf-chart-wrap").classList.add("hidden");

  try {
    const r = await fetch("/api/iperf/run", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ target, port, duration, mode, streams }),
    });
    const d = await r.json();

    if (!d.ok) {
      showStatus(document.getElementById("np-status"), `iperf3 錯誤：${d.error}`, true);
      return;
    }

    renderIperfResult(d, mode);
    showStatus(document.getElementById("np-status"), `iperf3 完成 — ${_resultSummary(d, mode)}`);
  } catch(e) {
    showStatus(document.getElementById("np-status"), `失敗：${e.message}`, true);
  } finally {
    setLoading(btn, false);
  }
}

function renderIperfResult(d, mode) {
  const cards = document.getElementById("iperf-result-cards");
  cards.classList.remove("hidden");

  // Show/hide relevant cards
  const dlCard   = document.getElementById("iperf-rc-dl");
  const ulCard   = document.getElementById("iperf-rc-ul");
  const udpCard  = document.getElementById("iperf-rc-udp");
  const lossCard = document.getElementById("iperf-rc-loss");

  if (mode === "udp") {
    dlCard.style.display  = "";
    ulCard.style.display  = "none";
    udpCard.style.display = "";
    lossCard.style.display= "";
    const mbps = d.mbps || 0;
    document.getElementById("iperf-dl").textContent = `${mbps} Mbps`;
    document.getElementById("iperf-dl").className   = "iperf-rcard-val " + _mbpsColor(mbps);
    document.getElementById("iperf-jitter").textContent    = `${d.jitter_ms ?? "—"} ms`;
    document.getElementById("iperf-pkt-loss").textContent  = `${d.lost_pct ?? "—"}%`;
    document.getElementById("iperf-retrans").textContent   = "—";
  } else {
    udpCard.style.display  = "none";
    lossCard.style.display = "none";
    const dl = d.dl_mbps ?? 0;
    const ul = d.ul_mbps ?? 0;

    dlCard.style.display = "";
    document.getElementById("iperf-dl").textContent = dl ? `${dl} Mbps` : "—";
    document.getElementById("iperf-dl").className   = "iperf-rcard-val " + _mbpsColor(dl);

    if (mode === "tcp_bidir" || (mode === "tcp_ul" && ul)) {
      ulCard.style.display = "";
      document.getElementById("iperf-ul").textContent = ul ? `${ul} Mbps` : "—";
      document.getElementById("iperf-ul").className   = "iperf-rcard-val " + _mbpsColor(ul);
    } else {
      ulCard.style.display = "none";
    }

    const rt = d.retransmits ?? 0;
    document.getElementById("iperf-retrans").textContent = rt;
    document.getElementById("iperf-retrans").className   = `iperf-rcard-val ${rt === 0 ? "good" : rt < 10 ? "" : "bad"}`;
  }

  // Throughput chart
  const series = d.series || [];
  if (series.length > 1) {
    document.getElementById("iperf-chart-wrap").classList.remove("hidden");
    renderIperfChart(series, d.rx_series || [], mode);
  }
}

function renderIperfChart(series, rxSeries, mode) {
  const ctx = document.getElementById("iperf-chart")?.getContext("2d");
  if (!ctx) return;
  if (iperfChart) { iperfChart.destroy(); iperfChart = null; }

  const labels  = series.map(p => `${p.t}s`);
  const txData  = series.map(p => p.mbps);
  const datasets = [{
    label: mode === "udp" ? "UDP Mbps" : mode === "tcp_ul" ? "Upload Mbps" : "Download Mbps",
    data: txData,
    borderColor:     "#3b82f6",
    backgroundColor: "rgba(59,130,246,.12)",
    borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: true,
  }];

  if (rxSeries.length) {
    datasets.push({
      label: "Upload Mbps",
      data:  rxSeries.map(p => p.mbps),
      borderColor:     "#22c55e",
      backgroundColor: "rgba(34,197,94,.06)",
      borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: false,
    });
  }

  iperfChart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        x: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { color: "#64748b", font: { size: 9 }, maxTicksLimit: 12 } },
        y: {
          min: 0,
          grid: { color: "rgba(255,255,255,.05)" },
          ticks: { color: "#64748b", font: { size: 9 } },
          title: { display: true, text: "Mbps", color: "#64748b", font: { size: 9 } },
        }
      },
      plugins: {
        legend: { labels: { color: "#94a3b8", font: { size: 9 }, boxWidth: 10 } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.y} Mbps` } }
      }
    }
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _mbpsColor(mbps) {
  if (!mbps) return "";
  if (mbps >= 500) return "good";
  if (mbps >= 50)  return "";
  return "bad";
}

function _modeLabel(mode) {
  return { tcp_dl: "TCP 下載", tcp_ul: "TCP 上傳", tcp_bidir: "TCP 雙向", udp: "UDP" }[mode] || mode;
}

function _resultSummary(d, mode) {
  if (mode === "udp") return `UDP ${d.mbps} Mbps  Jitter ${d.jitter_ms} ms  Loss ${d.lost_pct}%`;
  return `DL ${d.dl_mbps || "—"} Mbps  UL ${d.ul_mbps || "—"} Mbps  Retrans ${d.retransmits || 0}`;
}
