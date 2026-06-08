/* ═══════════════════════════════════════════════════════════════════════════
   monitor.js v2.1 — Multi-color per-AP RSSI lines, quality score card,
                     pause/resume scanning during interaction
   ═══════════════════════════════════════════════════════════════════════════ */

let monChart = null;
let _sse     = null;

document.getElementById("btn-mon-start").addEventListener("click", startMonitor);
document.getElementById("btn-mon-stop").addEventListener("click",  stopMonitor);

async function startMonitor() {
  const ssid     = document.getElementById("mon-ssid").value.trim();
  const interval = parseFloat(document.getElementById("mon-interval").value) || 2;

  clearMonCards();
  initMonChart();

  await fetch("/api/monitor/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ ssid: ssid || null, interval })
  });

  if (_sse) { _sse.close(); _sse = null; }
  _sse = new EventSource("/api/monitor/stream");
  _sse.onmessage = e => {
    try {
      const pt = JSON.parse(e.data);
      if (!pt.connected) return;
      updateMonChart(pt);
      updateMonCards(pt);
    } catch (_) {}
  };
}

async function stopMonitor() {
  if (_sse) { _sse.close(); _sse = null; }
  await fetch("/api/monitor/stop", { method: "POST" });
}

function initMonChart() {
  const ctx = document.getElementById("mon-chart").getContext("2d");
  if (monChart) { monChart.destroy(); monChart = null; }

  monChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "RSSI (dBm)", data: [],
          borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.07)",
          borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: true,
        },
        {
          label: "SNR (dB)", data: [],
          borderColor: "#22c55e", backgroundColor: "rgba(34,197,94,.04)",
          borderWidth: 1, pointRadius: 0, tension: 0.3, yAxisID: "y2",
        },
        {
          label: "品質分數", data: [],
          borderColor: "#a855f7", backgroundColor: "transparent",
          borderWidth: 1, pointRadius: 0, tension: 0.3, yAxisID: "y3",
          borderDash: [3, 3],
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { grid: { color: "rgba(255,255,255,.04)" }, ticks: { color: "#64748b", font: { size: 8 }, maxTicksLimit: 10, maxRotation: 0 } },
        y: {
          min: -100, max: -20,
          grid: { color: "rgba(255,255,255,.05)" },
          ticks: { color: "#64748b", font: { size: 8 } },
          title: { display: true, text: "RSSI (dBm)", color: "#64748b", font: { size: 8 } },
        },
        y2: {
          position: "right", min: 0, max: 60,
          grid: { display: false },
          ticks: { color: "#22c55e66", font: { size: 8 } },
          title: { display: true, text: "SNR", color: "#22c55e66", font: { size: 8 } },
        },
        y3: {
          position: "right", min: 0, max: 100, display: false,
        }
      },
      plugins: {
        legend: { labels: { color: "#94a3b8", font: { size: 9 }, boxWidth: 10 } },
        tooltip: {
          callbacks: {
            label: ctx => {
              if (ctx.dataset.label === "RSSI (dBm)") return ` RSSI: ${ctx.parsed.y} dBm`;
              if (ctx.dataset.label === "SNR (dB)")   return ` SNR: ${ctx.parsed.y} dB`;
              return ` 品質: ${ctx.parsed.y}/100`;
            }
          }
        }
      }
    }
  });

  // Threshold plugin
  const plugin = {
    id: "monThresh",
    afterDraw(chart) {
      const {ctx: c, scales: {y}} = chart;
      [[-65,"數據 -65","rgba(34,197,94,.45)"], [-67,"語音 -67","rgba(234,179,8,.45)"], [-70,"死角 -70","rgba(239,68,68,.45)"]].forEach(([v,l,col]) => {
        const yp = y.getPixelForValue(v);
        c.save(); c.strokeStyle = col; c.lineWidth = 1; c.setLineDash([2,4]);
        c.beginPath(); c.moveTo(chart.chartArea.left, yp); c.lineTo(chart.chartArea.right, yp); c.stroke();
        c.fillStyle = col.replace(".45",".8"); c.font = "8px -apple-system";
        c.fillText(l, chart.chartArea.left+4, yp-2); c.restore();
      });
    }
  };
  Chart.register(plugin);
  monChart.update();
}

const MAX_PTS = 120;

function updateMonChart(pt) {
  if (!monChart) return;
  const ds    = monChart.data;
  const label = new Date(pt.ts * 1000).toLocaleTimeString("zh-TW", { hour:"2-digit", minute:"2-digit", second:"2-digit" });

  ds.labels.push(label);
  ds.datasets[0].data.push(pt.rssi);
  ds.datasets[1].data.push(pt.snr);
  ds.datasets[2].data.push(pt.quality_score || 0);

  if (ds.labels.length > MAX_PTS) {
    ds.labels.shift();
    ds.datasets.forEach(d => d.data.shift());
  }
  monChart.update("none");

  // Threshold bar
  const pct = Math.max(0, Math.min(100, ((pt.rssi + 90) / 50) * 100));
  document.getElementById("mon-thresh-fill").style.cssText = `width:${pct}%;background:${rssiToColor(pt.rssi)}`;
  document.getElementById("mon-thresh-label").textContent  = getThreshLabel(pt.rssi, pt.snr);
}

function getThreshLabel(rssi, snr) {
  if (rssi >= -55) return `${rssi} dBm / SNR ${snr} dB — 優異`;
  if (rssi >= -65) return `${rssi} dBm / SNR ${snr} dB — 良好`;
  if (rssi >= -67) return `${rssi} dBm — 語音/漫遊臨界`;
  if (rssi >= -70) return `${rssi} dBm — 普通`;
  return `${rssi} dBm — ⚠ 弱訊，考慮補 AP`;
}

function updateMonCards(pt) {
  document.getElementById("mon-rssi").textContent   = `${pt.rssi} dBm`;
  document.getElementById("mon-rssi").style.color   = rssiToColor(pt.rssi);
  document.getElementById("mon-snr").textContent    = `${pt.snr} dB`;
  document.getElementById("mon-tx").textContent     = `${pt.tx_rate} Mbps`;
  document.getElementById("mon-qscore").textContent = `${pt.quality_score || 0}/100`;
  document.getElementById("mon-qscore").style.color = rssiToColor(pt.rssi);

  const data = monChart?.data.datasets[0].data || [];
  if (data.length) {
    document.getElementById("mon-min").textContent = `${Math.min(...data)} dBm`;
    document.getElementById("mon-max").textContent = `${Math.max(...data)} dBm`;
    document.getElementById("mon-avg").textContent = `${(data.reduce((a,b) => a+b, 0)/data.length).toFixed(1)} dBm`;
  }
}

function clearMonCards() {
  ["mon-rssi","mon-snr","mon-qscore","mon-tx","mon-min","mon-max","mon-avg"].forEach(id => {
    document.getElementById(id).textContent = "—";
    document.getElementById(id).style.color = "";
  });
  document.getElementById("mon-thresh-fill").style.width = "0";
  document.getElementById("mon-thresh-label").textContent = "等待資料…";
}
