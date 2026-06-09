/* ═══════════════════════════════════════════════════════════════════════════
   app.js — Tab routing, shared state, connection status poller v2.1
   ═══════════════════════════════════════════════════════════════════════════ */

window.APP = {
  networks:    [],
  analysis:    {},
  bands:       ["2.4GHz", "5GHz"],  // populated on init
  lastScanTs:  null,
};

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll(".nav-item").forEach(link => {
  link.addEventListener("click", e => {
    e.preventDefault();
    const tab = link.dataset.tab;
    document.querySelectorAll(".nav-item").forEach(l => l.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
    link.classList.add("active");
    document.getElementById(`tab-${tab}`).classList.add("active");
  });
});

// ── Band support detection ────────────────────────────────────────────────────
async function checkBandSupport() {
  try {
    const r = await fetch("/api/bands");
    const d = await r.json();
    APP.bands = d.bands || ["2.4GHz", "5GHz"];
    // Hide 6GHz UI elements if not supported
    if (!APP.bands.includes("6GHz")) {
      document.getElementById("panel-6ghz")?.classList.add("hidden");
      document.querySelectorAll('.band-cb input[value="6GHz"]').forEach(cb => {
        cb.parentElement.style.opacity = "0.4";
        cb.disabled = true;
      });
    }
  } catch (_) {}
}
checkBandSupport();

// ── WiFi interface selector ───────────────────────────────────────────────────
async function loadInterfaces() {
  const sel = document.getElementById("iface-select");
  if (!sel) return;
  try {
    const r = await fetch("/api/interfaces");
    const d = await r.json();
    const ifaces = d.interfaces || [];
    sel.innerHTML = ifaces.map(i =>
      `<option value="${i.name}">${i.name}${i.default ? " (預設)" : ""}${!i.active ? " ✗" : ""}</option>`
    ).join("");
    if (ifaces.length <= 1) {
      sel.parentElement.style.display = "none"; // 只有一張卡就隱藏
    }
  } catch (_) {
    sel.innerHTML = '<option value="">無法取得網卡</option>';
  }
}

document.getElementById("iface-select")?.addEventListener("change", async e => {
  const name = e.target.value;
  await fetch("/api/interface", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ name }),
  });
  // 切換後自動重新掃描
  document.getElementById("btn-scan")?.click();
});

loadInterfaces();

// ── Current connection poller ─────────────────────────────────────────────────
async function pollConnection() {
  try {
    const r = await fetch("/api/current");
    const d = await r.json();
    updateConnBadge(d);
  } catch (_) {}
}

function updateConnBadge(d) {
  const badge = document.getElementById("conn-badge");
  const none  = document.getElementById("conn-none");
  if (!d.connected) {
    badge.classList.add("hidden");
    none.classList.remove("hidden");
    return;
  }
  none.classList.add("hidden");
  badge.classList.remove("hidden");
  document.getElementById("conn-ssid").textContent   = d.ssid || "—";
  document.getElementById("conn-vendor").textContent = d.vendor || "";
  document.getElementById("conn-rssi").textContent   = `${d.rssi} dBm`;
  document.getElementById("conn-snr").textContent    = `SNR ${d.snr}`;
  document.getElementById("conn-band").textContent   = `${d.band} ch${d.channel}`;
  document.getElementById("conn-tx").textContent     = `Tx ${d.tx_rate} Mbps`;
  document.getElementById("conn-std").textContent    = d.standard || "";

  // Quality score bar
  const qs = d.quality_score || 0;
  const qbar = document.getElementById("conn-qbar");
  const qcolor = rssiToColor(d.rssi);
  qbar.style.setProperty("--qw", qs + "%");
  qbar.style.cssText = `flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden;position:relative`;
  qbar.innerHTML = `<div style="width:${qs}%;height:100%;background:${qcolor};border-radius:2px;transition:width .5s"></div>`;
  document.getElementById("conn-qscore").textContent = `${qs}/100`;
}

pollConnection();
setInterval(pollConnection, 5000);

// ── Band filter ───────────────────────────────────────────────────────────────
function getActiveBands() {
  return Array.from(document.querySelectorAll(".band-cb input:checked")).map(cb => cb.value);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
window.rssiToWidth = rssi => Math.max(0, Math.min(100, ((rssi + 90) / 50) * 100));
window.rssiToColor = rssi => {
  if (rssi >= -55) return "#22c55e";
  if (rssi >= -65) return "#84cc16";
  if (rssi >= -70) return "#eab308";
  if (rssi >= -75) return "#f97316";
  return "#ef4444";
};
window.snrColor = snr => {
  if (snr >= 30) return "#22c55e";
  if (snr >= 25) return "#84cc16";
  if (snr >= 20) return "#eab308";
  if (snr >= 15) return "#f97316";
  return "#ef4444";
};
window.escHtml = s => String(s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":'&#39;'})[c]);
window.showStatus = (el, msg, isError=false) => {
  el.textContent = msg;
  el.classList.remove("hidden","error");
  if (isError) el.classList.add("error");
};
window.setLoading = (btn, loading) => {
  if (loading) { btn._origHTML = btn.innerHTML; btn.innerHTML = '<div class="spinner"></div>'; btn.disabled = true; }
  else { btn.innerHTML = btn._origHTML || btn.innerHTML; btn.disabled = false; }
};
window.getActiveBands = getActiveBands;
