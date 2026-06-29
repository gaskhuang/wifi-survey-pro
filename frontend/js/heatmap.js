/* ═══════════════════════════════════════════════════════════════════════════
   heatmap.js v2.1 — Multi-metric layers, RBF backend, contour lines,
                     scan pause during click, JSON export, station count display
   ═══════════════════════════════════════════════════════════════════════════ */

let _fpLoaded   = false;
let _recordMode = false;
let _pendingPos = null;

// ── Floor plan upload ─────────────────────────────────────────────────────────
document.getElementById("fp-upload").addEventListener("change", async e => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async evt => {
    const b64 = evt.target.result;
    const img = new Image();
    img.onload = () => {
      const canvas = document.getElementById("fp-canvas");
      const wrap   = canvas.parentElement;
      const ratio  = img.width / img.height;
      const maxW   = wrap.clientWidth  - 4;
      const maxH   = wrap.clientHeight - 4;
      let w = maxW, h = maxW / ratio;
      if (h > maxH) { h = maxH; w = maxH * ratio; }
      canvas.width  = Math.floor(w);
      canvas.height = Math.floor(h);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      canvas.style.display = "block";
      document.getElementById("fp-hint").style.display    = "none";
      document.getElementById("heatmap-overlay").style.display = "none";
      _fpLoaded = true;
    };
    img.src = b64;
    try {
      await fetch("/api/heatmap/floorplan", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ image: b64 })
      });
    } catch(_) {}
  };
  reader.readAsDataURL(file);
  e.target.value = "";
});

// ── Record mode ───────────────────────────────────────────────────────────────
document.getElementById("btn-record-pt").addEventListener("click", () => {
  _recordMode = !_recordMode;
  const btn = document.getElementById("btn-record-pt");
  if (_recordMode) {
    btn.style.background = "var(--success)";
    btn.textContent      = "● 點擊地圖記錄量測點";
    // Pause background scanning to avoid race conditions
    fetch("/api/scan/pause", { method: "POST" });
  } else {
    btn.style.background = "";
    btn.innerHTML = `<svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:currentColor;fill:none;stroke-width:2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg> 記錄量測點`;
    fetch("/api/scan/resume", { method: "POST" });
  }
});

// ── Canvas click → open modal ─────────────────────────────────────────────────
document.getElementById("fp-canvas").addEventListener("click", async e => {
  if (!_recordMode) return;
  const canvas = e.currentTarget;
  const rect   = canvas.getBoundingClientRect();
  _pendingPos  = {
    x_pct: (e.clientX - rect.left) / canvas.width,
    y_pct: (e.clientY - rect.top)  / canvas.height,
  };
  // Auto-fill from current connection
  try {
    const r = await fetch("/api/current");
    const d = await r.json();
    if (d.connected) {
      document.getElementById("pt-rssi").value = d.rssi;
      document.getElementById("pt-snr").value  = d.snr;
      document.getElementById("pt-ssid").value = d.ssid || "";
    }
  } catch(_) {}
  document.getElementById("pt-modal").classList.remove("hidden");
});

// ── Modal: auto-read ─────────────────────────────────────────────────────────
document.getElementById("pt-auto-read").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/current");
    const d = await r.json();
    if (d.connected) {
      document.getElementById("pt-rssi").value = d.rssi;
      document.getElementById("pt-snr").value  = d.snr;
      document.getElementById("pt-ssid").value = d.ssid || "";
    }
  } catch(_) {}
});

document.getElementById("pt-cancel").addEventListener("click", () => {
  document.getElementById("pt-modal").classList.add("hidden");
  _pendingPos = null;
});

document.getElementById("pt-confirm").addEventListener("click", async () => {
  if (!_pendingPos) return;
  const rssi = parseInt(document.getElementById("pt-rssi").value) || -70;
  const snr  = parseInt(document.getElementById("pt-snr").value)  || 0;
  const ssid = document.getElementById("pt-ssid").value;
  document.getElementById("pt-modal").classList.add("hidden");

  // Also capture full AP scan at this point
  let scanResults = [];
  try {
    const sr = await fetch("/api/scan");
    const sd = await sr.json();
    scanResults = sd.networks || [];
  } catch(_) {}

  const r = await fetch("/api/heatmap/add", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({
      x: _pendingPos.x_pct, y: _pendingPos.y_pct,
      rssi, snr, ssid,
      scan_results: scanResults.slice(0, 20),  // store top 20 APs at this point
    })
  });
  const d = await r.json();
  if (d.ok) {
    drawDotOnCanvas(_pendingPos.x_pct, _pendingPos.y_pct, rssi, d.point.id);
    addMeasurementRow(d.point);
    updateMeasCount(d.total);
  }
  _pendingPos = null;
});

// ── Generate heatmap ──────────────────────────────────────────────────────────
document.getElementById("btn-gen-heatmap").addEventListener("click", async () => {
  const btn    = document.getElementById("btn-gen-heatmap");
  const metric = document.getElementById("hm-metric").value || "rssi";
  setLoading(btn, true);
  try {
    const canvas = document.getElementById("fp-canvas");
    const w = canvas.width  || 1000;
    const h = canvas.height || 700;
    const r = await fetch(`/api/heatmap/generate?w=${w}&h=${h}&labels=1&metric=${metric}&contour=1`);
    const d = await r.json();
    if (!d.ok || !d.image) throw new Error("量測點不足（至少需要 2 點）");

    document.getElementById("heatmap-img").src = d.image;
    document.getElementById("heatmap-overlay").style.display = "block";

    // 熱圖生成 → 自動產生覆蓋優化建議方案
    const pts = await (await fetch("/api/heatmap/points")).json();
    Reco.fetchAndRender("hm-reco", "heatmap", { measurements: pts.points || [] });
  } catch(e) {
    alert(`熱圖生成失敗：${e.message}`);
  } finally {
    setLoading(btn, false);
  }
});

// Switch metric → re-render heatmap if overlay is visible
document.getElementById("hm-metric").addEventListener("change", async () => {
  const overlay = document.getElementById("heatmap-overlay");
  if (overlay.style.display !== "none") {
    document.getElementById("btn-gen-heatmap").click();
  }
});

// ── Clear ─────────────────────────────────────────────────────────────────────
document.getElementById("btn-clear-pts").addEventListener("click", async () => {
  if (!confirm("確定清除所有量測點？")) return;
  await fetch("/api/heatmap/clear", { method: "POST" });
  document.getElementById("heatmap-overlay").style.display = "none";
  document.getElementById("meas-list").innerHTML = "";
  updateMeasCount(0);
  // Redraw floor plan
  const canvas = document.getElementById("fp-canvas");
  if (_fpLoaded) {
    const img = document.getElementById("heatmap-img");
    const src = img.src;
    if (src) {
      canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    }
  }
});

// ── Export ────────────────────────────────────────────────────────────────────
document.getElementById("btn-export-meas-csv").addEventListener("click",  () => window.location.href = "/api/heatmap/export_csv");
document.getElementById("btn-export-meas-json").addEventListener("click", () => window.location.href = "/api/heatmap/export_json");

// ── Helpers ───────────────────────────────────────────────────────────────────
function drawDotOnCanvas(xPct, yPct, rssi, id) {
  const canvas = document.getElementById("fp-canvas");
  const ctx    = canvas.getContext("2d");
  const px     = xPct * canvas.width;
  const py     = yPct * canvas.height;
  const color  = rssiToColor(rssi);
  ctx.beginPath();
  ctx.arc(px, py, 8, 0, Math.PI * 2);
  ctx.fillStyle   = color + "cc";
  ctx.strokeStyle = "#fff";
  ctx.lineWidth   = 1.5;
  ctx.fill(); ctx.stroke();
  ctx.fillStyle = "#fff"; ctx.font = "bold 8px -apple-system"; ctx.textAlign = "center";
  ctx.fillText(`${rssi}`, px, py + 3);
}

function addMeasurementRow(pt) {
  const list = document.getElementById("meas-list");
  const div  = document.createElement("div");
  div.className = "meas-item";
  div.dataset.id = pt.id;
  const sc = pt.station_count != null ? ` ↔${pt.station_count}` : "";
  div.innerHTML = `
    <span class="mi-id">#${pt.id+1}</span>
    <span class="mi-rssi" style="color:${pt.color||rssiToColor(pt.rssi)}">${pt.rssi}dBm${sc}</span>
    <span style="color:var(--text-3);font-size:9px">${pt.label}</span>
    <span class="mi-del" onclick="delPoint(${pt.id})">✕</span>`;
  list.appendChild(div);
}

window.delPoint = async id => {
  await fetch(`/api/heatmap/remove/${id}`, { method: "DELETE" });
  document.querySelector(`.meas-item[data-id="${id}"]`)?.remove();
  const r = await fetch("/api/heatmap/points");
  const d = await r.json();
  updateMeasCount(d.points.length);
};

function updateMeasCount(n) {
  document.getElementById("meas-count").textContent = n;
  const el = document.getElementById("rck-meas-count");
  if (el) el.textContent = n;
}
