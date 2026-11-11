/* ═══════════════════════════════════════════════════════════════════════════
   lan.js — 設備盤點分頁
   · 掃描網段 → 逐台顯示名稱 / IP / 廠牌 / 類型 / 型號與系統 / 服務
   · 點一列展開這台設備的完整資料（各協定回報的原始內容）
   · 搜尋、依類型篩選；摘要卡 + 風險建議（/api/recommend context=lan）
   · 每台可即時 ping / 埠掃描；匯出 CSV / JSON / PDF
   · 現場診斷跑完的盤點結果也會帶到這一頁（LAN.setResult）
   ═══════════════════════════════════════════════════════════════════════════ */
const LAN = (() => {

  let _es       = null;
  let _hosts    = [];      // 逐台累積
  let _rowByIp  = {};      // ip → <tr>，供邊掃邊即時更新
  let _result   = null;    // 完成後的完整結果
  let _ifaceLoaded = false;
  let _typeFilter = "";    // 點類型 chip 篩選
  let _open = new Set();   // 已展開詳情的 ip
  const COLS = 7;

  const PHASE_TXT = {
    ping:        "掃描主機中…（探測網段內每個 IP）",
    portscan:    "埠掃描中…（辨識服務）",
    identify:    "詢問設備名稱 / 型號中…（mDNS・NetBIOS・UPnP・SNMP）",
    fingerprint: "彙整設備資料中…",
  };
  const SRC_LABEL = { ping: "Ping", arp: "ARP", port: "埠掃描", mdns: "mDNS", netbios: "NetBIOS",
                      upnp: "UPnP", snmp: "SNMP" };

  const $ = id => document.getElementById(id);
  const esc = s => window.escHtml(s);
  const sub = s => s ? `<div class="lan-sub">${s}</div>` : "";

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
    _hosts = []; _rowByIp = {}; _result = null; _typeFilter = ""; _open = new Set();
    $("lan-tbody").innerHTML = "";
    $("lan-typebar").innerHTML = "";
    $("lan-reco").hidden = true;
    $("lan-progress-wrap").classList.remove("hidden");
    $("lan-progress-bar").style.width = "0%";
    $("lan-live-hint").textContent = "掃描中…";
    const btn = $("btn-lan-scan"); btn.disabled = true;
    showStatus(`掃描 ${cidr}${deep ? "（深度辨識）" : ""}…`);

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
      if (d.host) {
        if (d.host._phase) {                       // 階段切換 → 更新提示文字
          const t = PHASE_TXT[d.host._phase] || "";
          $("lan-live-hint").textContent = d.host.count ? `${t}（${d.host.count} 台）` : t;
          return;
        }
        upsertRow(d.host);                         // 邊掃邊即時新增 / 更新該台
        $("lan-count").textContent = Object.keys(_rowByIp).length;
      }
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
    _es.onerror = () => {
      if (_result) return;                 // 已完成，忽略關閉時的 error
      showStatus("與伺服器的連線中斷，掃描已停止，請重試", true);
      stopScan();
    };
  }

  function stopScan() {
    if (_es) { _es.close(); _es = null; }
    $("btn-lan-scan").disabled = false;
    $("lan-progress-wrap").classList.add("hidden");
    $("lan-live-hint").textContent = "";
  }

  function finishScan() {
    stopScan();
    showResult(true);
    showStatus(`完成：盤點到 ${_hosts.length} 台設備`);
  }

  // 顯示一份完整結果（剛掃完，或由現場診斷 / 上次掃描帶入）
  function showResult(fetchReco) {
    _hosts = _result.hosts || [];
    _rowByIp = {};
    $("lan-count").textContent   = _result.alive_count ?? _hosts.length;
    $("lan-gw").textContent      = _result.gateway || "—";
    $("lan-subnet").textContent  = _result.subnet || "—";
    $("lan-scanned").textContent = `${_result.total}/${_result.total}`;
    const named = _hosts.filter(h => h.name || h.model).length;
    $("lan-named").textContent = _hosts.length ? `${named}/${_hosts.length}` : "—";
    if (_result.subnet && !$("lan-cidr").value) $("lan-cidr").value = _result.subnet;
    renderTable();
    renderTypeBar(_result.summary || {});
    if (_result.assessment) Reco.render("lan-reco", _result.assessment, { title: "設備盤點發現與建議" });
    else if (fetchReco) Reco.fetchAndRender("lan-reco", "lan", { scan: _result }, { scroll: false });
  }

  function setResult(r) {
    if (!r || !Array.isArray(r.hosts)) return;
    if (_es) return;                               // 這一頁正在掃描 → 不覆蓋
    _result = r; _typeFilter = ""; _open = new Set();
    showResult(true);
    showStatus(`已帶入盤點結果：${_hosts.length} 台設備`);
  }

  // ── 表格 ────────────────────────────────────────────────────────────────────
  function rowHTML(h) {
    const ports = h.open_ports || [];
    const labels = h.service_labels || [];
    const svcParts = [];
    if (labels.length) svcParts.push(labels.map(esc).join("、"));
    if (ports.length) svcParts.push(ports.map((p, i) =>
      `${esc(p)}${(h.services || [])[i] ? "/" + esc(h.services[i]) : ""}`).join("、"));
    const gw = h.is_gateway ? ' <span class="lan-gw-dot" title="預設閘道">⚑</span>' : "";
    const partial = h.partial;
    const name = h.name || (partial ? "" : "—");
    const hostSub = h.hostname && h.hostname !== h.name ? esc(h.hostname) : "";
    const osTxt = h.os || (h.os_hint ? `${h.os_hint}（推測）` : "");
    const rtt = h.rtt_ms != null ? `${esc(h.rtt_ms)} ms` : "";
    return `
      <td class="lan-name"><span class="lan-caret">▸</span>${esc(name)}${sub(hostSub)}</td>
      <td>${esc(h.ip)}${gw}${sub(rtt)}</td>
      <td class="lan-vendor">${esc(h.vendor || "未知")}${sub(esc(h.mac || ""))}</td>
      <td>${typeBadge(h)}</td>
      <td class="lan-model">${esc(h.model || (partial ? "" : "—"))}${sub(esc(osTxt))}</td>
      <td class="svc">${svcParts.join("<br>") || "—"}</td>
      <td><div class="lan-act">
        <button data-act="ping" data-ip="${esc(h.ip)}">Ping</button>
        <button data-act="ports" data-ip="${esc(h.ip)}">埠掃描</button>
      </div></td>`;
  }

  // 這台設備的完整資料：每個協定回報了什麼
  function detailHTML(h) {
    const d = h.details || {};
    const kv = (k, v) => (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) ? ""
      : `<div class="lan-kv"><span>${esc(k)}</span><b>${esc(Array.isArray(v) ? v.join("、") : v)}</b></div>`;
    const group = (title, body) => body ? `<div class="lan-dgroup"><h4>${esc(title)}</h4>${body}</div>` : "";
    const up = d.upnp || {}, sn = d.snmp || {}, nb = d.netbios || {}, md = d.mdns || {};
    const txt = md.txt || {};
    const pick = (o, keys) => keys.map(k => o[k] ? `${k}=${o[k]}` : "").filter(Boolean);
    const txtRows = Object.entries(txt).map(([svc, t]) => {
      const kept = pick(t, ["model", "am", "md", "fn", "ty", "product", "osvers", "srcvers", "manufacturer",
                            "usb_mfg", "usb_mdl", "note", "adminurl", "id"]);
      return kept.length ? kv(svc, kept.join("；")) : "";
    }).join("");
    return `<td colspan="${COLS}"><div class="lan-dgrid">
      ${group("基本資料",
        kv("IP 位址", h.ip) + kv("MAC 位址", h.mac ? `${h.mac}${h.mac_random ? "（隨機 / 私人位址）" : ""}` : "") +
        kv("廠牌", h.vendor) + kv("設備類型", h.type) +
        kv("回應延遲", h.rtt_ms != null ? `${h.rtt_ms} ms` : "") + kv("TTL", h.ttl) +
        kv("資料來源", (h.sources || []).map(s => SRC_LABEL[s] || s)))}
      ${group("識別結果",
        kv("設備名稱", h.name) + kv("型號", h.model) + kv("作業系統", h.os || (h.os_hint ? `${h.os_hint}（依 TTL 推測）` : "")) +
        kv("主機名（DNS 反查）", h.hostname) + kv("mDNS 主機名", md.hostname) +
        kv("工作群組", h.workgroup) + kv("序號", h.serial))}
      ${group("服務",
        kv("宣告的服務", h.service_labels) +
        kv("開放的埠", (h.open_ports || []).map((p, i) => `${p}${(h.services || [])[i] ? "/" + h.services[i] : ""}`)))}
      ${group("UPnP 回報",
        kv("名稱", up.friendly) + kv("製造商", up.manufacturer) + kv("型號", [up.model, up.model_number].filter(Boolean).join(" ")) +
        kv("說明", up.model_desc) + kv("伺服器", up.server) + kv("網際網路閘道（IGD）", up.igd ? "是" : ""))}
      ${group("SNMP 回報（community: public）", kv("sysName", sn.name) + kv("sysDescr", sn.descr) + kv("sysLocation", sn.location))}
      ${group("NetBIOS 回報", kv("電腦名稱", nb.name) + kv("工作群組", nb.workgroup) + kv("檔案分享", nb.file_server ? "有" : ""))}
      ${group("管理介面 / 連線橫幅",
        (d.web || []).map(w => kv(`網頁（${w.port}）`, [w.title, w.realm, w.server].filter(Boolean).join("｜"))).join("") +
        kv("SSH", d.ssh) + kv("FTP", d.ftp))}
      ${group("mDNS 服務資料", kv("服務實例", md.names) + txtRows)}
    </div></td>`;
  }

  function matches(h) {
    if (_typeFilter && h.type !== _typeFilter) return false;
    const q = $("lan-search").value.trim().toLowerCase();
    if (!q) return true;
    return [h.ip, h.mac, h.name, h.hostname, h.vendor, h.type, h.model, h.os,
            (h.service_labels || []).join(" "), (h.open_ports || []).join(" ")]
      .some(v => String(v || "").toLowerCase().includes(q));
  }

  function upsertRow(h) {
    let tr = _rowByIp[h.ip];
    const i = _hosts.findIndex(x => x.ip === h.ip);
    if (i >= 0) _hosts[i] = h; else _hosts.push(h);
    if (tr) {                          // 已有該台 → 就地補上/更新細節
      tr.innerHTML = rowHTML(h);
    } else {                           // 新探到 → 立即冒出一列
      tr = document.createElement("tr");
      tr.dataset.ip = h.ip;
      tr.innerHTML = rowHTML(h);
      $("lan-tbody").appendChild(tr);
      _rowByIp[h.ip] = tr;
    }
  }

  function renderTable() {
    const tb = $("lan-tbody");
    _rowByIp = {};
    if (!_hosts.length) {
      tb.innerHTML = `<tr class="empty-row"><td colspan="${COLS}">此網段未發現存活設備</td></tr>`;
      return;
    }
    const shown = _hosts.filter(matches);
    if (!shown.length) {
      tb.innerHTML = `<tr class="empty-row"><td colspan="${COLS}">沒有符合篩選條件的設備</td></tr>`;
    } else {
      tb.innerHTML = shown.map(h =>
        `<tr data-ip="${esc(h.ip)}"${_open.has(h.ip) ? ' class="open"' : ""}>${rowHTML(h)}</tr>` +
        (_open.has(h.ip) ? `<tr class="lan-detail lan-info">${detailHTML(h)}</tr>` : "")).join("");
      tb.querySelectorAll("tr[data-ip]").forEach(tr => { _rowByIp[tr.dataset.ip] = tr; });
    }
    $("lan-live-hint").textContent = shown.length !== _hosts.length ? `顯示 ${shown.length} / ${_hosts.length} 台` : "";
  }

  function renderTypeBar(summary) {
    const entries = Object.entries(summary).sort((a, b) => b[1] - a[1]);
    $("lan-typebar").innerHTML = entries.map(([t, c]) =>
      `<button class="chip${_typeFilter === t ? " active" : ""}" data-type="${esc(t)}" title="只顯示這個類型">${esc(t)} <b>${esc(c)}</b></button>`).join("") +
      (_typeFilter ? `<button class="chip clear" data-type="">✕ 顯示全部</button>` : "");
  }

  function toggleDetail(tr) {
    const ip = tr.dataset.ip;
    const h = _hosts.find(x => x.ip === ip);
    if (!h || h.partial) return;
    const next = tr.nextElementSibling;
    if (next?.classList.contains("lan-info")) {
      next.remove(); tr.classList.remove("open"); _open.delete(ip);
      return;
    }
    if (next?.classList.contains("lan-detail")) next.remove();    // Ping / 埠掃描的結果列
    const d = document.createElement("tr");
    d.className = "lan-detail lan-info";
    d.innerHTML = detailHTML(h);
    tr.after(d); tr.classList.add("open"); _open.add(ip);
  }

  // ── 逐台工具：ping / 埠掃描 ──────────────────────────────────────────────────
  async function rowAction(act, ip, btn) {
    const tr = btn.closest("tr");
    if (tr.nextElementSibling?.classList.contains("lan-detail")) {
      tr.nextElementSibling.remove(); tr.classList.remove("open"); _open.delete(ip);
    }
    const detail = document.createElement("tr");
    detail.className = "lan-detail";
    detail.innerHTML = `<td colspan="${COLS}">執行中…</td>`;
    tr.after(detail);
    try {
      if (act === "ping") {
        const d = (await postJSON("/api/lan/ping", { ip })).result;
        detail.innerHTML = `<td colspan="${COLS}">Ping ${esc(ip)}：${
          d.alive ? `✅ 平均 ${esc(d.rtt_ms ?? "—")} ms（${esc(d.recv)}/${esc(d.sent)}，掉包 ${esc(d.loss_pct)}%）`
                  : `❌ 無回應（掉包 ${esc(d.loss_pct)}%）`}</td>`;
      } else {
        const d = (await postJSON("/api/lan/portscan", { ip })).result;
        const ports = (d.open_ports || []).map(p => `${esc(p.port)}${p.service ? "/" + esc(p.service) : ""}`).join("、");
        detail.innerHTML = `<td colspan="${COLS}">埠掃描 ${esc(ip)}：${ports || "無開放埠"}</td>`;
      }
    } catch (e) {
      detail.innerHTML = `<td colspan="${COLS}" style="color:#f87171">失敗：${esc(e.message)}</td>`;
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

  // 第一次開這一頁：帶入預設網段；若先前（或現場診斷）已掃過，直接顯示那份結果
  async function onShow() {
    loadIface();
    if (_result || _es) return;
    try {
      const d = await (await fetch("/api/lan/result")).json();
      if (d.ok && d.result?.hosts && !_result && !_es) {
        _result = d.result;
        showResult(true);
      }
    } catch (_) {}
  }

  // ── 事件綁定 ────────────────────────────────────────────────────────────────
  $("btn-lan-scan").addEventListener("click", startScan);
  $("btn-lan-csv").addEventListener("click", () => {
    if (!_result) { showStatus("請先掃描", true); return; }
    downloadURL("/api/lan/export?fmt=csv", "lan_hosts.csv").catch(e => showStatus(e.message, true));
  });
  $("btn-lan-json").addEventListener("click", () => {
    if (!_result) { showStatus("請先掃描", true); return; }
    downloadURL("/api/lan/export?fmt=json", "lan_hosts.json").catch(e => showStatus(e.message, true));
  });
  $("lan-tbody").addEventListener("click", e => {
    const b = e.target.closest("button[data-act]");
    if (b) { rowAction(b.dataset.act, b.dataset.ip, b); return; }
    const tr = e.target.closest("tr[data-ip]");
    if (tr && !window.getSelection().toString()) toggleDetail(tr);   // 選取文字時不要收合
  });
  $("lan-search").addEventListener("input", renderTable);
  $("lan-typebar").addEventListener("click", e => {
    const c = e.target.closest("[data-type]");
    if (!c) return;
    _typeFilter = c.dataset.type === _typeFilter ? "" : c.dataset.type;
    renderTypeBar(_result?.summary || {});
    renderTable();
  });
  window.addEventListener("tab:activated", e => { if (e.detail?.tab === "lan") onShow(); });

  return { startScan, setResult, getResult: () => _result };
})();
