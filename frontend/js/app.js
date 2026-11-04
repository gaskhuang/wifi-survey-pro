/* ═══════════════════════════════════════════════════════════════════════════
   app.js — Tab routing, shared state, connection status poller v2.1
   ═══════════════════════════════════════════════════════════════════════════ */

// 圖表預設字級（Chart.js 預設 12px）跟著介面放大 20%
if (window.Chart) Chart.defaults.font.size = 14;

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
    // 通知各分頁（延遲初始化 / 切入重繪）
    window.dispatchEvent(new CustomEvent("tab:activated", { detail: { tab } }));
  });
});

// 分頁延遲初始化：該分頁第一次被開啟時才執行 fn（若載入時已是可見分頁則立即執行）
window.onTabFirstActive = (tab, fn) => {
  if (document.getElementById(`tab-${tab}`)?.classList.contains("active")) { fn(); return; }
  const h = e => {
    if (e.detail?.tab !== tab) return;
    window.removeEventListener("tab:activated", h);
    fn();
  };
  window.addEventListener("tab:activated", h);
};

// fetch + JSON + 逾時（AbortController），避免單一卡住的請求拖垮鏈式輪詢
window.fetchJSON = async (url, opts = {}, timeoutMs = 10000) => {
  const ac = new AbortController();
  const to = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...opts, signal: ac.signal });
    return await r.json();
  } finally {
    clearTimeout(to);
  }
};

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
      `<option value="${escHtml(i.name)}">${escHtml(i.name)}${i.default ? " (預設)" : ""}${!i.active ? " ✗" : ""}</option>`
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
// 單一鏈式 setTimeout：上一次回來才排下一次（不重疊）、8 秒逾時、視窗隱藏時暫停。
// 結果發布到 APP.current 並廣播 "conn:update"，其他模組不必各自再打 /api/current。
const CONN_POLL_MS    = 5000;
const CONN_TIMEOUT_MS = 8000;
APP.current   = null;
APP.currentTs = 0;
let _connInflight = null;
let _connTimer    = null;

function fetchCurrent() {
  if (_connInflight) return _connInflight;          // in-flight 共用，不重複打
  _connInflight = fetchJSON("/api/current", {}, CONN_TIMEOUT_MS)
    .then(d => {
      APP.current   = d;
      APP.currentTs = Date.now();
      updateConnBadge(d);
      window.dispatchEvent(new CustomEvent("conn:update", { detail: d }));
      return d;
    })
    .finally(() => { _connInflight = null; });
  return _connInflight;
}
// 取目前連線：maxAgeMs 內的快取直接用，否則抓一次（與輪詢共用同一請求）
window.getCurrentConn = (maxAgeMs = CONN_POLL_MS + 1000) =>
  (APP.current && Date.now() - APP.currentTs <= maxAgeMs)
    ? Promise.resolve(APP.current) : fetchCurrent();

function scheduleConnPoll(ms = CONN_POLL_MS) {
  clearTimeout(_connTimer);
  _connTimer = document.hidden ? null : setTimeout(pollConnection, ms);
}

async function pollConnection() {
  _connTimer = null;
  if (document.hidden) return;                       // 隱藏時不排程；visible 時再恢復
  try { await fetchCurrent(); } catch (_) {}
  scheduleConnPoll();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearTimeout(_connTimer); _connTimer = null; }
  else { clearTimeout(_connTimer); pollConnection(); }   // 回到前景立即更新
});

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
window.escHtml = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":'&#39;'})[c]);
window.showStatus = (el, msg, isError=false) => {
  el.textContent = msg;
  el.classList.remove("hidden","error");
  if (isError) el.classList.add("error");
};
// 存檔：App 視窗（WKWebView）只認 <a download> 觸發的下載，且下載是非同步的，
// 太早 revokeObjectURL 會讓存檔失敗 → 統一走這裡，延後一分鐘才釋放。
window.saveBlob = (blob, filename) => {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename || "download";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
};
// 下載伺服器產生的檔案（CSV/JSON/PDF）：不能用 location.href，
// JSON 會直接在 App 視窗裡打開、蓋掉整個介面
window.downloadURL = async (url, fallbackName, opts = {}) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `下載失敗（HTTP ${r.status}）`;
    try { const j = await r.json(); if (j.error) msg = j.error; } catch (_) {}
    throw new Error(msg);
  }
  const cd = r.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  saveBlob(await r.blob(), name);
};
window.setLoading = (btn, loading) => {
  if (loading) { btn._origHTML = btn.innerHTML; btn.innerHTML = '<div class="spinner"></div>'; btn.disabled = true; }
  else { btn.innerHTML = btn._origHTML || btn.innerHTML; btn.disabled = false; }
};
window.getActiveBands = getActiveBands;
