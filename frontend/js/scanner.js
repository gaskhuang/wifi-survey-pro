/* ═══════════════════════════════════════════════════════════════════════════
   scanner.js v3.0 — WiFi Explorer-style UI
   Features: filter tabs, search, sortable columns, spectrum chart, status bar
   ═══════════════════════════════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────────────────────────────────
let _currentBssid = "";         // connected AP BSSID
let _activeFilter = "all";      // band/security filter
let _secFilter    = "";         // open / secure
let _searchText   = "";
const sortState   = { key: "signal_pct", dir: "desc" };

// ── Boot ──────────────────────────────────────────────────────────────────────
document.getElementById("btn-scan").addEventListener("click", doScan);
document.getElementById("btn-export-csv-scan").addEventListener("click", () => window.location.href = "/api/export/networks");
document.getElementById("btn-export-json-scan").addEventListener("click", () => window.location.href = "/api/export/json");

// Filter tabs
document.querySelectorAll(".we-tab[data-filter]").forEach(btn => {
  btn.addEventListener("click", () => {
    const f = btn.dataset.filter;
    if (["all","2.4GHz","5GHz","6GHz"].includes(f)) {
      _activeFilter = f;
      document.querySelectorAll(".we-tab[data-filter='all'],.we-tab[data-filter='2.4GHz'],.we-tab[data-filter='5GHz'],.we-tab[data-filter='6GHz']").forEach(b => b.classList.remove("active"));
    } else {
      // open / secure: toggle
      if (_secFilter === f) { _secFilter = ""; btn.classList.remove("active"); }
      else { _secFilter = f; document.querySelectorAll(".we-tab[data-filter='open'],.we-tab[data-filter='secure']").forEach(b => b.classList.remove("active")); }
    }
    btn.classList.toggle("active", f === _activeFilter || f === _secFilter);
    rerender();
  });
});

// Search
document.getElementById("scan-search").addEventListener("input", e => {
  _searchText = e.target.value.toLowerCase().trim();
  rerender();
});

// Column sort
document.querySelectorAll("#net-table th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    sortState.dir = sortState.key === key
      ? (sortState.dir === "asc" ? "desc" : "asc")
      : (_isNumericKey(key) ? "desc" : "asc");
    sortState.key = key;
    _updateSortHeaders();
    rerender();
  });
});
_updateSortHeaders();

// ── Scan ──────────────────────────────────────────────────────────────────────
async function doScan() {
  const btn    = document.getElementById("btn-scan");
  const status = document.getElementById("scan-status");
  setLoading(btn, true);
  showStatus(status, "Scanning for Wi-Fi networks…");

  try {
    const r = await fetch("/api/scan");
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "Scan failed");

    APP.networks = d.networks;

    // Update current BSSID from connection badge
    const conn = await (await fetch("/api/current")).json();
    _currentBssid = conn.bssid || "";

    rerender();

    let msg = `Scan complete — ${d.count} networks found`;
    if (d.needs_location) msg += " · Enable Location Services to see SSIDs";
    showStatus(status, msg, d.needs_location);
    updateReportCounts();

    // 掃描完成 → 自動產生鄰居環境優化建議方案
    Reco.fetchAndRender("scan-reco", "scan", { networks: APP.networks, current: conn });
  } catch(e) {
    showStatus(status, `Error: ${e.message}`, true);
  } finally {
    setLoading(btn, false);
  }
}

// ── Filter + sort pipeline ────────────────────────────────────────────────────
function getDisplayNets() {
  let nets = APP.networks;

  // Band filter
  if (_activeFilter !== "all")
    nets = nets.filter(n => n.band === _activeFilter);

  // Security filter
  if (_secFilter === "open")
    nets = nets.filter(n => n.is_open);
  else if (_secFilter === "secure")
    nets = nets.filter(n => !n.is_open);

  // Search
  if (_searchText)
    nets = nets.filter(n =>
      (n.ssid||"").toLowerCase().includes(_searchText) ||
      (n.bssid||"").toLowerCase().includes(_searchText) ||
      (n.vendor||"").toLowerCase().includes(_searchText) ||
      (n.security||"").toLowerCase().includes(_searchText)
    );

  return _sortNets(nets);
}

function rerender() {
  const nets = getDisplayNets();
  nets.forEach((n, i) => { n._idx = i; });   // stable per-render index for cross-linking
  renderNetTable(nets);
  Spectrum.update(nets, _currentBssid);
  updateStatusBar(nets);
}

// ── Table renderer ────────────────────────────────────────────────────────────
function renderNetTable(nets) {
  const tbody = document.getElementById("net-tbody");
  if (!nets.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="13">No networks match the current filter</td></tr>';
    return;
  }

  tbody.innerHTML = nets.map(n => {
    const isConn  = n.bssid === _currentBssid;
    const pct     = n.signal_pct || 0;
    const sigColor = _signalColor(pct);
    const ssidHtml = _ssidCell(n);
    const vendorHtml = n.vendor
      ? `<span style="font-size:10px">${escHtml(n.vendor)}</span>`
      : `<span style="color:var(--text-3);font-size:10px">—</span>`;
    const maxRate = n.max_rate != null
      ? `<span style="color:var(--text-2)">${n.max_rate} Mbps</span>`
      : "—";
    const gen = n.gen >= 4
      ? `<span class="gen-badge" style="color:${_genColor(n.gen)}">${n.gen}</span>`
      : "—";
    const secHtml = `<span style="font-size:10px;color:var(--text-2)">${escHtml(n.security||"")}</span>`;
    const seenHtml = `<span style="font-size:10px">${n.seen_ago||"—"}</span>`;
    const bssidDisp = n.bssid.startsWith("<") ? `<span style="color:var(--text-3);font-size:9px">${escHtml(n.bssid)}</span>` : escHtml(n.bssid);

    return `<tr class="net-row${isConn?" connected":""}" data-bssid="${escHtml(n.bssid)}" data-idx="${n._idx}">
      <td class="col-bar-cell"><div class="col-bar-inner" style="background:${n.net_color}"></div></td>
      <td>${bssidDisp}</td>
      <td>${ssidHtml}</td>
      <td>${vendorHtml}</td>
      <td>
        <div class="we-signal">
          <span class="we-signal-pct" style="color:${sigColor}">${pct}%</span>
          <div class="we-signal-bar"><div class="we-signal-fill" style="width:${pct}%;background:${sigColor}"></div></div>
        </div>
      </td>
      <td style="text-align:center;font-weight:600">${n.channel}</td>
      <td style="color:var(--text-2)">${n.width} MHz</td>
      <td style="color:var(--text-2)">${n.band}</td>
      <td style="font-family:monospace;font-size:10px;color:var(--text-2)">${escHtml(n.mode||"")}</td>
      <td style="text-align:center">${gen}</td>
      <td>${secHtml}</td>
      <td style="text-align:right">${maxRate}</td>
      <td>${seenHtml}</td>
    </tr>`;
  }).join("");

  // Click row → highlight row + highlight spectrum
  document.querySelectorAll("#net-tbody tr.net-row").forEach((tr, i) => {
    tr.addEventListener("click", () => {
      document.querySelectorAll("#net-tbody tr.net-row.selected").forEach(r => r.classList.remove("selected"));
      tr.classList.add("selected");
      Spectrum.setHighlight(nets[i]);
      updateStatusBar(nets);
    });
  });
}

function _ssidCell(n) {
  const lock = n.is_open ? "" : `<span class="lock-icon">🔒</span>`;
  if (!n.ssid || n.ssid === "<Hidden>" || n.ssid.startsWith("<")) {
    return `${lock}<span class="ssid-hidden">Hidden Network</span>${n.is_new ? '<span class="new-badge">NEW</span>' : ""}`;
  }
  return `${lock}<strong>${escHtml(n.ssid)}</strong>${n.is_new ? '<span class="new-badge">NEW</span>' : ""}`;
}

// ── Status bar ────────────────────────────────────────────────────────────────
function updateStatusBar(displayedNets) {
  const total     = APP.networks.length;
  const displayed = displayedNets.length;
  const selected  = document.querySelectorAll("#net-tbody tr.net-row.selected").length;
  const pct       = total ? Math.round(displayed / total * 100) : 0;
  document.getElementById("sb-total").textContent     = total;
  document.getElementById("sb-displayed").textContent = displayed;
  document.getElementById("sb-pct").textContent       = `${pct}%`;
  document.getElementById("sb-selected").textContent  = `${selected} (${total ? Math.round(selected/total*100) : 0}%)`;
}

// ── Sort helpers ──────────────────────────────────────────────────────────────
function _isNumericKey(k) {
  return ["rssi","snr","signal_pct","quality_score","channel","gen","max_rate","station_count","channel_util_pct"].includes(k);
}

function _sortNets(nets) {
  const { key, dir } = sortState;
  return [...nets].sort((a, b) => {
    let va = a[key], vb = b[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number")
      return dir === "asc" ? va - vb : vb - va;
    // seen_ago: special time-aware sort
    if (key === "seen_ago") {
      va = a.last_seen || 0; vb = b.last_seen || 0;
      return dir === "asc" ? va - vb : vb - va;
    }
    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    const c = va.localeCompare(vb, "zh-Hant", { numeric: true });
    return dir === "asc" ? c : -c;
  });
}

function _updateSortHeaders() {
  document.querySelectorAll("#net-table th[data-sort]").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === sortState.key)
      th.classList.add(sortState.dir === "asc" ? "sort-asc" : "sort-desc");
  });
}

// ── Color helpers ─────────────────────────────────────────────────────────────
function _signalColor(pct) {
  if (pct >= 75) return "#22c55e";
  if (pct >= 55) return "#84cc16";
  if (pct >= 40) return "#f59e0b";
  if (pct >= 25) return "#f97316";
  return "#ef4444";
}

function _genColor(gen) {
  const c = { 4: "#6366f1", 5: "#3b82f6", 6: "#22c55e", 7: "#a855f7" };
  return c[gen] || "var(--text-2)";
}

function updateReportCounts() {
  const el = document.getElementById("rck-scan-count");
  if (el) el.textContent = APP.networks.length;
}
