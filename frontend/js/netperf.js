/* ═══════════════════════════════════════════════════════════════════════════
   netperf.js — Network performance: ping/DNS/HTTP latency without iperf3
   (inspired by python-wifi-survey-heatmap per-point testing)
   ═══════════════════════════════════════════════════════════════════════════ */

let npChart = null;
const npHistory = { ping: [], dns: [], http: [] };
const MAX_HIST  = 30;

document.getElementById("btn-netperf").addEventListener("click", runNetperf);

async function runNetperf() {
  const btn  = document.getElementById("btn-netperf");
  const stat = document.getElementById("np-status");
  const host = document.getElementById("np-host").value.trim() || "8.8.8.8";
  const dns  = document.getElementById("np-dns").value.trim()  || "8.8.8.8";
  const n    = document.getElementById("np-samples").value    || 5;

  setLoading(btn, true);
  showStatus(stat, `正在測試 Ping(${host}) / DNS(${dns}) / HTTP… (${n} 次)`);

  try {
    const r = await fetch(`/api/netperf?host=${encodeURIComponent(host)}&dns=${encodeURIComponent(dns)}&n=${n}`, { timeout: 35000 });
    const d = await r.json();

    // Update cards
    updateCard("np-ping",   "np-ping-g",  d.ping_ms,  "ms", d.ping_grade,  pingColor(d.ping_ms));
    updateCard("np-jitter", null,          d.ping_jitter_ms, "ms", null,   jitterColor(d.ping_jitter_ms));
    updateCard("np-loss",   null,          d.ping_loss_pct, "%",   null,   lossColor(d.ping_loss_pct));
    updateCard("np-dns-ms", "np-dns-g",   d.dns_ms,   "ms", d.dns_grade,   dnsColor(d.dns_ms));
    updateCard("np-http",   null,          d.http_ms,  "ms", null,          httpColor(d.http_ms));

    // Push to history
    if (d.ping_ms != null) npHistory.ping.push(d.ping_ms);
    if (d.dns_ms  != null) npHistory.dns.push(d.dns_ms);
    if (d.http_ms != null) npHistory.http.push(d.http_ms);
    if (npHistory.ping.length > MAX_HIST) { npHistory.ping.shift(); npHistory.dns.shift(); npHistory.http.shift(); }

    updateNpChart();

    const errs = d.errors?.length ? ` | 錯誤: ${d.errors.join(", ")}` : "";
    showStatus(stat, `測試完成：Ping ${d.ping_ms ?? "—"} ms / DNS ${d.dns_ms ?? "—"} ms / HTTP ${d.http_ms ?? "—"} ms${errs}`);
  } catch(e) {
    showStatus(stat, `測試失敗：${e.message}`, true);
  } finally {
    setLoading(btn, false);
  }
}

function updateCard(valId, gradeId, val, unit, grade, color) {
  const el = document.getElementById(valId);
  if (val != null) {
    el.textContent   = `${val} ${unit}`;
    el.style.color   = color || "var(--text-1)";
  } else {
    el.textContent = "—";
    el.style.color = "var(--text-3)";
  }
  if (gradeId) {
    const gel = document.getElementById(gradeId);
    if (gel) { gel.textContent = grade || ""; gel.style.color = color || "var(--text-2)"; }
  }
}

function updateNpChart() {
  const ctx = document.getElementById("np-chart")?.getContext("2d");
  if (!ctx) return;

  const labels = Array.from({length: npHistory.ping.length}, (_, i) => `#${i+1}`);

  if (npChart) { npChart.destroy(); npChart = null; }
  npChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Ping (ms)",  data: npHistory.ping, borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.07)", borderWidth: 1.5, pointRadius: 3, tension: 0.3, fill: true },
        { label: "DNS (ms)",   data: npHistory.dns,  borderColor: "#22c55e", backgroundColor: "transparent",           borderWidth: 1,   pointRadius: 2, tension: 0.3 },
        { label: "HTTP (ms)",  data: npHistory.http, borderColor: "#f59e0b", backgroundColor: "transparent",           borderWidth: 1,   pointRadius: 2, tension: 0.3 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        x: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { color: "#64748b", font: { size: 9 } } },
        y: {
          min: 0,
          grid: { color: "rgba(255,255,255,.05)" },
          ticks: { color: "#64748b", font: { size: 9 } },
          title: { display: true, text: "ms", color: "#64748b", font: { size: 9 } },
        }
      },
      plugins: {
        legend: { labels: { color: "#94a3b8", font: { size: 10 }, boxWidth: 10 } },
        annotation: {}
      }
    }
  });
}

// Color helpers
function pingColor(ms)   { if (!ms) return "var(--text-3)"; return ms<=10?"#22c55e":ms<=30?"#84cc16":ms<=80?"#eab308":"#ef4444"; }
function jitterColor(ms) { if (!ms) return "var(--text-3)"; return ms<=2 ?"#22c55e":ms<=5 ?"#84cc16":ms<=15?"#eab308":"#ef4444"; }
function lossColor(pct)  { if (pct==null) return "var(--text-3)"; return pct===0?"#22c55e":pct<0.5?"#84cc16":pct<2?"#eab308":"#ef4444"; }
function dnsColor(ms)    { if (!ms) return "var(--text-3)"; return ms<=20?"#22c55e":ms<=50?"#84cc16":ms<=150?"#eab308":"#ef4444"; }
function httpColor(ms)   { if (!ms) return "var(--text-3)"; return ms<=100?"#22c55e":ms<=300?"#84cc16":ms<=1000?"#eab308":"#ef4444"; }
