/* ═══════════════════════════════════════════════════════════════════════════
   channels.js v2.1 — Bandwidth-aware channel blocks, 6GHz split, DFS shading,
                       non-overlapping channel highlights, Wi-Fi 7 badges
   ═══════════════════════════════════════════════════════════════════════════ */

document.getElementById("btn-analyze").addEventListener("click", doAnalyze);

async function doAnalyze() {
  const btn = document.getElementById("btn-analyze");
  setLoading(btn, true);
  try {
    const nets = APP.networks.length ? APP.networks : null;
    const r    = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nets ? { networks: nets } : {})
    });
    const d = await r.json();
    if (!d.ok) throw new Error("分析失敗");

    APP.analysis = d.analysis;
    if (!APP.networks.length) APP.networks = d.networks;

    renderSummaryCards(d.analysis.summary);
    renderBandwidthAwareChart("2.4GHz", d.networks, "ch-chart-24");
    renderBandwidthAwareChart("5GHz",   d.networks, "ch-chart-5");
    renderBandwidthAwareChart("6GHz",   d.networks, "ch-chart-6");
    renderBestChannels("2.4GHz", d.analysis["2.4GHz"], "best-24");
    renderBestChannels("5GHz",   d.analysis["5GHz"],   "best-5");
    renderBestChannels("6GHz",   d.analysis["6GHz"],   "best-6");
    renderRecommendations(d.analysis.recommendations);
    updateReportPreview(d.analysis);
  } catch(e) {
    alert(`頻道分析失敗：${e.message}`);
  } finally {
    setLoading(btn, false);
  }
}

function renderSummaryCards(s) {
  if (!s) return;
  document.getElementById("csum-total").textContent = s.total_networks || "—";
  document.getElementById("csum-24").textContent    = (s.bands||{})["2.4GHz"] || 0;
  document.getElementById("csum-5").textContent     = (s.bands||{})["5GHz"]  || 0;
  document.getElementById("csum-6").textContent     = (s.bands||{})["6GHz"]  || 0;
  document.getElementById("csum-coi").textContent   = s.co_channel_hotspots || 0;
  const w7el = document.getElementById("csum-wifi7");
  if (w7el) {
    w7el.textContent = s.has_wifi7_candidate ? "✓" : "—";
    w7el.style.color = s.has_wifi7_candidate ? "var(--success)" : "var(--text-3)";
  }
}

/**
 * Bandwidth-aware channel occupancy chart.
 * Each AP's bar spans ch_block[0] → ch_block[1], showing true spectral footprint.
 */
function renderBandwidthAwareChart(band, networks, canvasId) {
  const bandNets = networks.filter(n => n.band === band);
  if (!bandNets.length) return;

  // Determine channel range for this band
  let allChans, xLabels;
  if (band === "2.4GHz") {
    allChans = Array.from({length: 11}, (_, i) => i + 1);
  } else if (band === "5GHz") {
    allChans = [36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,149,153,157,161,165];
  } else {
    // 6 GHz: only show lower band (ch1-125) to avoid overcrowding
    allChans = Array.from({length: 32}, (_, i) => 1 + i*4).filter(c => c <= 125);
  }
  xLabels = allChans.map(c => `${c}`);

  const DFS_5G = new Set([52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144]);
  const NON_OVERLAP_24 = new Set([1, 6, 11]);

  // Build per-channel congestion score (bandwidth-aware spreading)
  const congestion = {};
  const bgColors   = {};
  allChans.forEach(c => { congestion[c] = 0; });

  bandNets.forEach(n => {
    const [cStart, cEnd] = n.ch_block || [n.channel, n.channel];
    const subChans = allChans.filter(c => c >= cStart && c <= cEnd);
    if (!subChans.length) subChans.push(n.channel);
    const spread = 1.0 / subChans.length;  // spread the "weight" across sub-channels
    subChans.forEach(c => {
      if (congestion[c] !== undefined) congestion[c] += spread * 20;
    });
  });

  const data = allChans.map(c => Math.min(100, congestion[c] || 0));
  const colors = allChans.map(c => {
    const score = congestion[c] || 0;
    if (score === 0) return "rgba(59,130,246,.1)";
    if (band === "5GHz" && DFS_5G.has(c)) {
      const a = Math.min(.85, score/100 + .2);
      return `rgba(249,115,22,${a})`;
    }
    const a = Math.min(.85, score/100 + .2);
    if (score >= 60) return `rgba(239,68,68,${a})`;
    if (score >= 30) return `rgba(234,179,8,${a})`;
    return `rgba(34,197,94,${a})`;
  });

  const ctx = document.getElementById(canvasId)?.getContext("2d");
  if (!ctx) return;
  const existing = window[`_chart_${canvasId}`];
  if (existing) existing.destroy();

  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: xLabels,
      datasets: [{
        data, backgroundColor: colors, borderWidth: 0, borderRadius: 2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        y: {
          min: 0, max: 100,
          grid:  { color: "rgba(255,255,255,.04)" },
          ticks: { color: "#64748b", font: { size: 8 } },
          title: { display: true, text: "壅塞", color: "#64748b", font: { size: 8 } },
        },
        x: {
          grid: { display: false },
          ticks: {
            color: c => {
              const ch = allChans[c.index];
              if (band === "2.4GHz" && NON_OVERLAP_24.has(ch)) return "#60a5fa";
              return "#64748b";
            },
            font: { size: 8 }, maxRotation: 0,
            callback: (_, idx) => {
              const ch = allChans[idx];
              if (band === "2.4GHz") return NON_OVERLAP_24.has(ch) ? `●${ch}` : `${ch}`;
              return `${ch}`;
            }
          }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => `頻道 ${allChans[items[0].dataIndex]}`,
            label: item => {
              const ch   = allChans[item.dataIndex];
              const nets = bandNets.filter(n => {
                const [s, e] = n.ch_block || [n.channel, n.channel];
                return ch >= s && ch <= e;
              });
              if (!nets.length) return " 空閒";
              return [
                ` 壅塞: ${item.parsed.y.toFixed(0)}/100`,
                ...nets.map(n => ` · ${n.ssid} (${n.rssi}dBm ${n.width}MHz)`)
              ];
            }
          }
        }
      }
    }
  });
  window[`_chart_${canvasId}`] = chart;
}

function renderBestChannels(band, bandData, elId) {
  if (!bandData) return;
  const el   = document.getElementById(elId);
  const best = (bandData.best_channels || []).slice(0, 4);
  if (!best.length) { el.innerHTML = '<span style="color:var(--text-3);font-size:10px">無數據</span>'; return; }

  el.innerHTML = `<span style="font-size:10px;color:var(--text-2);margin-right:5px">建議：</span>` +
    best.map((b, i) => {
      const cls = i === 0 ? "good" : b.congestion > 40 ? "warn" : "";
      const dfs = b.is_dfs ? " ⚠DFS" : "";
      return `<span class="best-ch-badge ${cls}" title="${b.reason||''}">ch${b.channel}${i===0?" ★":""}${dfs}</span>`;
    }).join("");
}

function renderRecommendations(recs) {
  if (!recs?.length) return;
  const el = document.getElementById("recs-list");
  el.innerHTML = recs.map(r => `
    <div class="rec-item ${r.level||''}">
      <span class="rec-band">${r.band}</span>
      <span class="rec-action">${r.action}</span>
      <span class="rec-detail">${r.value} — ${r.detail}</span>
    </div>`).join("");
}

function updateReportPreview(analysis) {
  const el = document.getElementById("rp-content");
  if (!el) return;
  const s = analysis.summary || {};
  const recs = (analysis.recommendations||[]).map(r => `<li>${r.band} ${r.action}：${r.value}</li>`).join("");
  el.innerHTML = `
    <p><strong>偵測到 ${s.total_networks||0} 個鄰近網路</strong>
       （2.4G：${(s.bands||{})["2.4GHz"]||0}、5G：${(s.bands||{})["5GHz"]||0}、6G：${(s.bands||{})["6GHz"]||0}）</p>
    <p style="margin-top:6px">同頻干擾熱區：${s.co_channel_hotspots||0} | Wi-Fi 7：${s.has_wifi7_candidate?"是":"否"} | 6GHz：${s.has_6ghz?"是":"否"}</p>
    ${recs ? `<ul style="margin-top:10px;padding-left:16px;line-height:2">${recs}</ul>` : ""}
  `;
}
