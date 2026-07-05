/* ═══════════════════════════════════════════════════════════════════════════
   lan.js — 有線 LAN 盤查分頁
   · 掃描網段 → 逐台顯示（IP/MAC/廠牌/類型/主機名/開放埠）
   · 摘要卡 + 類型統計 chip + 優化/風險建議（走 /api/recommend context=lan）
   · 每台可即時 ping / 埠掃描
   · 匯出 CSV / JSON
   ═══════════════════════════════════════════════════════════════════════════ */
const LAN = (() => {

  let _es       = null;
  let _hosts    = [];      // 逐台累積
  let _result   = null;    // 完成後的完整結果
  let _ifaceLoaded = false;

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));

  const RISK_TYPES = new Set(["路由器/防火牆"]);
  function typeBadge(h) {
    const t = h.type || "不明";
    let cls = "lan-type-badge";
    if (t.startsWith("本機")) cls += " self";
    return `<span class="${cls}">${esc(t)}</span>`;
  }

  // ── 載入預設網段 ────────────────────────────────────────────────────────────
  async function loadIface() {
    if (_ifaceLoaded) return;
    try {
      const d = await (await fetch("/api/lan/interfaces")).json();
      const ifc = (d.interfaces || [])[0];
      if (ifc && !$("lan-cidr").value) $("lan-cidr").value = ifc.subnet_cidr;
      _ifaceLoaded = true;
    } catch (_) {}
  }

  // ── 掃描 ────────────────────────────────────────────────────────────────────
  async function startScan() {
    if (!$("lan-cidr").value.trim()) await loadIface();   // 未填則自動帶入預設網段
    const cidr = $("lan-cidr").value.trim();
    if (!cidr) { showStatus("請輸入網段（例：192.168.1.0/24）", true); return; }
    const deep = $("lan-deep").checked;

    if (_es) { _es.close(); _es = null; }
    _hosts = []; _result = null;
    $("lan-tbody").innerHTML = "";
    $("lan-reco").hidden = true;
    $("lan-progress-wrap").classList.remove("hidden");
    $("lan-progress-bar").style.width = "0%";
    $("lan-live-hint").textContent = "掃描中…";
    const btn = $("btn-lan-scan"); btn.disabled = true;
    showStatus(`掃描 ${cidr}${deep ? "（深度指紋）" : ""}…`);

    _es = new EventSource(`/api/lan/scan/stream?subnet=${encodeURIComponent(cidr)}&deep=${deep ? 1 : 0}`);

    _es.addEventListener("start", e => {
      const d = JSON.parse(e.data);
      $("lan-subnet").textContent = d.subnet;
    });

    _es.addEventListener("progress", e => {
      const d = JSON.parse(e.data);
      if (d.total) {
        const pct = Math.min(100, Math.round(d.done / d.total * 100));
        $("lan-progress-bar").style.width = pct + "%";
        $("lan-scanned").textContent = `${d.done}/${d.total}`;
      }
      if (d.host) { _hosts.push(d.host); appendRow(d.host); $("lan-count").textContent = _hosts.length; }
    });

    _es.addEventListener("done", e => {
      _result = JSON.parse(e.data);
      finishScan();
    });
    _es.addEventListener("error", e => {
      let msg = "掃描發生錯誤";
      try { msg = JSON.parse(e.data).error || msg; } catch (_) {}
      showStatus(msg, true);
      stopScan();
    });
    _es.onerror = () => { if (_result) return; stopScan(); };
  }

  function stopScan() {
    if (_es) { _es.close(); _es = null; }
    $("btn-lan-scan").disabled = false;
    $("lan-progress-wrap").classList.add("hidden");
    $("lan-live-hint").textContent = "";
  }

  function finishScan() {
    stopScan();
    // 用完整結果重繪（排序後）
    _hosts = _result.hosts || _hosts;
    $("lan-count").textContent   = _result.alive_count ?? _hosts.length;
    $("lan-gw").textContent      = _result.gateway || "—";
    $("lan-subnet").textContent  = _result.subnet || "—";
    $("lan-scanned").textContent = `${_result.total}/${_result.total}`;
    renderTable();
    renderTypeBar(_result.summary || {});
    showStatus(`完成：盤點到 ${_hosts.length} 台設備`);
    // 優化 / 風險建議
    Reco.fetchAndRender("lan-reco", "lan", { scan: _result }, { scroll: false });
  }

  // ── 表格 ────────────────────────────────────────────────────────────────────
  function rowHTML(h) {
    const svc = (h.open_ports || []).length
      ? (h.services || []).map((s, i) => `${h.open_ports[i]}${s ? "/" + esc(s) : ""}`).join("、")
      : "—";
    const gw = h.is_gateway ? ' <span class="lan-gw-dot" title="預設閘道">⚑</span>' : "";
    return `
      <td>${esc(h.ip)}${gw}</td>
      <td>${esc(h.mac || "—")}</td>
      <td>${esc(h.vendor || "未知")}</td>
      <td>${typeBadge(h)}</td>
      <td>${esc(h.hostname || "—")}</td>
      <td class="svc">${svc}</td>
      <td><div class="lan-act">
        <button data-act="ping" data-ip="${esc(h.ip)}">Ping</button>
        <button data-act="ports" data-ip="${esc(h.ip)}">埠掃描</button>
      </div></td>`;
  }

  function appendRow(h) {
    const tb = $("lan-tbody");
    const tr = document.createElement("tr");
    tr.innerHTML = rowHTML(h);
    tb.appendChild(tr);
  }

  function renderTable() {
    const tb = $("lan-tbody");
    if (!_hosts.length) {
      tb.innerHTML = '<tr class="empty-row"><td colspan="7">此網段未發現存活設備</td></tr>';
      return;
    }
    tb.innerHTML = _hosts.map(h => `<tr>${rowHTML(h)}</tr>`).join("");
  }

  function renderTypeBar(summary) {
    const entries = Object.entries(summary).sort((a, b) => b[1] - a[1]);
    $("lan-typebar").innerHTML = entries.map(([t, c]) =>
      `<span class="chip">${esc(t)} <b>${c}</b></span>`).join("");
  }

  // ── 逐台工具：ping / 埠掃描 ──────────────────────────────────────────────────
  async function rowAction(act, ip, btn) {
    const tr = btn.closest("tr");
    // 移除舊的 detail 列
    if (tr.nextElementSibling?.classList.contains("lan-detail")) tr.nextElementSibling.remove();
    const detail = document.createElement("tr");
    detail.className = "lan-detail";
    detail.innerHTML = `<td colspan="7">執行中…</td>`;
    tr.after(detail);
    try {
      if (act === "ping") {
        const d = (await postJSON("/api/lan/ping", { ip })).result;
        detail.innerHTML = `<td colspan="7">Ping ${esc(ip)}：${
          d.alive ? `✅ 平均 ${d.rtt_ms ?? "—"} ms（${d.recv}/${d.sent}，掉包 ${d.loss_pct}%）`
                  : `❌ 無回應（掉包 ${d.loss_pct}%）`}</td>`;
      } else {
        const d = (await postJSON("/api/lan/portscan", { ip })).result;
        const ports = (d.open_ports || []).map(p => `${p.port}${p.service ? "/" + esc(p.service) : ""}`).join("、");
        detail.innerHTML = `<td colspan="7">埠掃描 ${esc(ip)}：${ports ? esc(ports) : "無開放埠"}</td>`;
      }
    } catch (e) {
      detail.innerHTML = `<td colspan="7" style="color:#f87171">失敗：${esc(e.message)}</td>`;
    }
  }

  async function postJSON(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  // ── 狀態列 ──────────────────────────────────────────────────────────────────
  function showStatus(msg, err = false) {
    const el = $("lan-status");
    el.textContent = msg;
    el.classList.remove("hidden");
    el.style.color = err ? "#f87171" : "";
  }

  // ── 事件綁定 ────────────────────────────────────────────────────────────────
  $("btn-lan-scan").addEventListener("click", startScan);
  $("btn-lan-csv").addEventListener("click", () => {
    if (!_result) { showStatus("請先掃描", true); return; }
    window.location.href = "/api/lan/export?fmt=csv";
  });
  $("btn-lan-json").addEventListener("click", () => {
    if (!_result) { showStatus("請先掃描", true); return; }
    window.location.href = "/api/lan/export?fmt=json";
  });
  $("lan-tbody").addEventListener("click", e => {
    const b = e.target.closest("button[data-act]");
    if (b) rowAction(b.dataset.act, b.dataset.ip, b);
  });
  // 切到本分頁時載入預設網段
  document.querySelector('.nav-item[data-tab="lan"]')
    ?.addEventListener("click", () => setTimeout(loadIface, 60));

  return { startScan };
})();
