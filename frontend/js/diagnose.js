/* ═══════════════════════════════════════════════════════════════════════════
   diagnose.js — 現場診斷首頁（一鍵看完整個網路 + AI 建議行動）
   EventSource → /api/diagnose/stream，依事件更新進度，完成後渲染：
   · 數值儀表卡片（訊號、測速、延遲、穩定度、設備數、ISP）
   · 網路環境（對外線路 / NAT 層數、區域網路 / DHCP / DNS、注意事項）
   · 設備盤點（名稱 / 廠牌 / 類型 / 型號）
   · 頻道佔用長條圖
   · AI 建議行動清單（🔴 立即處理 / 🟡 建議調整 / 🟢 狀態正常）
   ═══════════════════════════════════════════════════════════════════════════ */

(() => {
  const DG_STEPS = [
    { key: "scan",      label: "Wi-Fi 環境掃描" },
    { key: "analysis",  label: "頻道分析" },
    { key: "current",   label: "目前連線狀態" },
    { key: "env",       label: "網路環境（閘道・DHCP・DNS・對外線路）" },
    { key: "netperf",   label: "Ping / DNS 延遲測試" },
    { key: "speedtest", label: "網路速度測試（上傳/下載）" },
    { key: "stability", label: "連線穩定度測試" },
    { key: "lan",       label: "設備盤點" },   // 放在量測之後：掃描會干擾延遲與穩定度的數字
    { key: "ai",        label: "AI 智慧分析" },
  ];

  let _es      = null;
  let _data    = {};
  let _stepEls = {};

  document.getElementById("btn-dg-start").addEventListener("click", startDiagnose);
  document.getElementById("btn-dg-pdf").addEventListener("click", exportPDF);

  // ── 匯出 PDF 診斷報告 ────────────────────────────────────────────────────────
  async function exportPDF() {
    if (!Object.keys(_data).length) { alert("請先執行診斷再匯出報告。"); return; }
    const btn = document.getElementById("btn-dg-pdf");
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<div class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,.3);border-top-color:#fff"></div> 產生報告中…`;
    try {
      const r = await fetch("/api/diagnose/report", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(_data),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
      saveBlob(blob, `wifi-diagnosis-${ts}.pdf`);
    } catch (e) {
      alert("報告產生失敗：" + e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }

  function startDiagnose() {
    const duration = document.getElementById("dg-duration").value || "30";
    _data = {};

    const btn = document.getElementById("btn-dg-start");
    btn.disabled = true;
    btn.querySelector(".dg-bigbtn-label").textContent = "診斷中…";
    document.getElementById("dg-result").classList.add("hidden");
    document.getElementById("dg-progress").classList.remove("hidden");
    document.getElementById("btn-dg-pdf").classList.add("hidden");
    document.getElementById("dg-hero").classList.add("compact");     // 進度與結果要在第一屏
    renderSteps();

    _es = new EventSource(`/api/diagnose/stream?duration=${duration}&speedtest=1&ai=1`);

    _es.addEventListener("step", e => {
      const d  = JSON.parse(e.data);
      const el = _stepEls[d.key];
      if (!el) return;
      if (d.status === "running") {
        el.className = "diag-step running";
        el.innerHTML = `<span class="step-icon"></span>${escHtml(d.label)}`;
      } else if (d.status === "ok") {
        el.className = "diag-step ok";
        el.innerHTML = `<span class="step-icon"></span>${escHtml(d.label)}`;
      } else {
        el.className = "diag-step fail";
        el.innerHTML = `<span class="step-icon"></span>${escHtml(d.label)}` +
          (d.error ? ` <span style="font-size: calc(12px * var(--fs, 1));color:var(--text-3)">（${escHtml(d.error)}）</span>` : "");
      }
    });

    _es.addEventListener("progress", e => {
      const d  = JSON.parse(e.data);
      const el = _stepEls[d.key];
      if (!el || !el.classList.contains("running")) return;
      const extra = d.key === "stability"
        ? `（${Math.round(d.elapsed)} / ${escHtml(d.total)} 秒）`
        : d.key === "lan" ? `（已掃描 ${escHtml(d.elapsed)} 秒）`
        : `（已分析 ${escHtml(d.elapsed)} 秒）`;
      const lbl = DG_STEPS.find(s => s.key === d.key)?.label || "";
      el.innerHTML = `<span class="step-icon"></span>${lbl} <span style="font-size: calc(12px * var(--fs, 1));color:var(--text-3)">${extra}</span>`;
    });

    _es.addEventListener("partial", e => {
      const d = JSON.parse(e.data);
      _data[d.key] = d.data;
      if (d.key === "lan" && typeof LAN !== "undefined") LAN.setResult(d.data);   // 「設備盤點」分頁同步
    });

    _es.addEventListener("done", () => { stopES(); renderResult(); });
    _es.onerror = () => {
      stopES();
      if (Object.keys(_data).length) renderResult();
      else {
        document.getElementById("dg-steps").innerHTML =
          `<div class="diag-step fail"><span class="step-icon"></span>診斷連線中斷，請重試</div>`;
        resetBtn();
      }
    };
  }

  function stopES() { if (_es) { _es.close(); _es = null; } }

  function resetBtn() {
    const btn = document.getElementById("btn-dg-start");
    btn.disabled = false;
    const has = Object.keys(_data).length > 0;
    btn.querySelector(".dg-bigbtn-label").textContent = has ? "重新診斷" : "開始診斷";
    document.getElementById("dg-hero").classList.toggle("compact", has);
    document.getElementById("btn-dg-pdf").classList.toggle("hidden", !has);
  }

  function renderSteps() {
    const wrap = document.getElementById("dg-steps");
    wrap.innerHTML = `<div class="diag-steps-grid"></div>`;
    const grid = wrap.firstElementChild;
    _stepEls = {};
    DG_STEPS.forEach(s => {
      const el = document.createElement("div");
      el.className = "diag-step";
      el.innerHTML = `<span class="step-icon" style="opacity:.3">○</span><span style="opacity:.4">${s.label}</span>`;
      grid.appendChild(el);
      _stepEls[s.key] = el;
    });
  }

  // ── 結果渲染 ────────────────────────────────────────────────────────────────
  function renderResult() {
    resetBtn();
    document.getElementById("dg-progress").classList.add("hidden");
    document.getElementById("dg-result").classList.remove("hidden");
    renderCards();
    renderEnv();
    renderDevices();
    renderAPs();
    drawChannelChart();
    renderAI();
    if (_data.scan?.length) APP.networks = _data.scan;
    if (_data.analysis)     APP.analysis = _data.analysis;
  }

  function renderCards() {
    const cur  = _data.current   || {};
    const np   = _data.netperf   || {};
    const spd  = _data.speedtest || {};
    const stab = _data.stability || {};
    const lan  = _data.lan;
    const wan  = (_data.env || {}).wan;

    const card = (label, val, sub = "", color = "") => `
      <div class="card"><div class="card-label">${label}</div>
        <div class="card-val" ${color ? `style="color:${color}"` : ""}>${val}</div>
        ${sub ? `<div class="card-sub">${sub}</div>` : ""}</div>`;

    const fmt = (v, unit) => v != null ? `${escHtml(v)}<span style="font-size: calc(13px * var(--fs, 1));color:var(--text-2)"> ${unit}</span>` : "—";

    document.getElementById("dg-cards").innerHTML = [
      card("訊號 RSSI", cur.connected ? fmt(cur.rssi, "dBm") : "未連線",
           escHtml(cur.ssid || ""), cur.connected ? rssiToColor(cur.rssi) : "#ef4444"),
      card("SNR", fmt(cur.snr, "dB"), "", cur.snr != null ? snrColor(cur.snr) : ""),
      card("下載", fmt(spd.down_mbps, "Mbps")),
      card("上傳", fmt(spd.up_mbps, "Mbps")),
      card("Ping", fmt(np.ping_ms, "ms"), np.ping_jitter_ms != null ? `Jitter ${escHtml(np.ping_jitter_ms)} ms` : ""),
      card("掉包", fmt(np.ping_loss_pct, "%"),
           "", np.ping_loss_pct > 0 ? "#ef4444" : "#22c55e"),
      card("穩定度", stab.score != null
           ? `${escHtml(stab.score)}<span style="font-size: calc(13px * var(--fs, 1));color:var(--text-2)"> /100</span>` : "—",
           escHtml(stab.grade?.label || ""), escHtml(stab.grade?.color || "")),
      lan ? card("設備", `${escHtml(lan.alive_count ?? 0)}<span style="font-size: calc(13px * var(--fs, 1));color:var(--text-2)"> 台</span>`,
                 escHtml(Object.entries(lan.summary || {}).sort((a, b) => b[1] - a[1]).slice(0, 2)
                   .map(([t, c]) => `${t} ${c}`).join("、"))) : "",
      wan ? card("對外線路", wan.online ? `<span style="font-size: calc(17px * var(--fs, 1))">${escHtml(wan.isp || "已連線")}</span>` : "離線",
                 escHtml(wan.online ? [wan.city, wan.country].filter(Boolean).join(", ") : "無法連上網際網路"),
                 wan.online ? "" : "#ef4444") : "",
    ].join("");
  }

  // ── 網路環境：對外線路 / 區域網路 / 注意事項 ────────────────────────────────
  const SEV_TXT = { critical: "嚴重", warning: "注意", info: "提醒", good: "正常" };
  const kv = (k, v) => (v === undefined || v === null || v === "") ? ""
    : `<div class="lan-kv"><span>${escHtml(k)}</span><b>${escHtml(v)}</b></div>`;

  function renderEnv() {
    const el = document.getElementById("dg-env");
    const env = _data.env;
    if (!env) { el.innerHTML = ""; return; }
    const lan = env.lan || {}, wan = env.wan || {}, nat = wan.nat || {};
    const dhcp = lan.dhcp || {};
    const lease = dhcp.lease_sec
      ? (dhcp.lease_sec >= 3600 ? `${+(dhcp.lease_sec / 3600).toFixed(1)} 小時` : `${Math.round(dhcp.lease_sec / 60)} 分鐘`) : "";
    const natTxt = !wan.online ? "" : nat.double_nat ? `雙重 NAT（${(nat.routers || []).length} 層路由器）`
      : (nat.private_hops ? "單層 NAT（正常）" : "");
    const hops = (nat.hops || []).filter(h => h.ip).map(h =>
      `<code>${escHtml(h.ip)}</code>${h.kind !== "公網" ? `<span style="color:var(--text-3)">（${escHtml(h.kind)}）</span>` : ""}`).join(" → ");
    const notes = (env.issues || []).map(i => `
      <div class="dg-note"><span class="tag ${escHtml(i.severity)}">${SEV_TXT[i.severity] || ""}</span>
        <div><b>${escHtml(i.title)}</b>${i.problem ? `　${escHtml(i.problem)}` : ""}
          ${i.recommendation ? `<div class="rec">→ ${escHtml(i.recommendation)}</div>` : ""}</div></div>`).join("");
    el.innerHTML = `
      <div class="dg-envbox"><h3>對外線路</h3>
        ${kv("狀態", wan.online ? "已連上網際網路" : "無法連上網際網路")}
        ${kv("ISP", wan.isp)}${kv("公網 IP", wan.public_ip)}
        ${kv("所在地", [wan.city, wan.country].filter(Boolean).join(", "))}
        ${kv("NAT", natTxt)}${kv("電信級 NAT", nat.cgnat ? "疑似（沒有獨立公網 IP）" : "")}
        ${kv("IPv6", wan.online ? (wan.ipv6 ? "可用" : "不可用") : "")}
        ${kv("強制登入頁", wan.captive_portal ? "有（需先登入）" : "")}
        ${hops ? `<div class="dg-hops">對外路徑：${hops}</div>` : ""}
      </div>
      <div class="dg-envbox"><h3>區域網路</h3>
        ${kv("連線方式", lan.link)}${kv("本機 IP", lan.ip)}${kv("網段", lan.subnet)}
        ${kv("閘道", lan.gateway ? `${lan.gateway}${lan.gateway_vendor ? `（${lan.gateway_vendor}）` : ""}` : "無")}
        ${kv("DNS", (lan.dns || []).join("、"))}
        ${kv("DHCP 伺服器", dhcp.server)}${kv("DHCP 租約", lease)}${kv("網域", dhcp.domain)}
      </div>
      ${notes ? `<div class="dg-envbox wide"><h3>網路環境判讀</h3>${notes}</div>` : ""}`;
  }

  // ── 設備盤點摘要（完整資料在「設備盤點」分頁）──────────────────────────────
  function renderDevices() {
    const sec = document.getElementById("dg-dev-section");
    const lan = _data.lan;
    if (!lan || !Array.isArray(lan.hosts)) { sec.classList.add("hidden"); return; }
    sec.classList.remove("hidden");
    document.getElementById("dg-dev-count").textContent = lan.alive_count ?? lan.hosts.length;
    document.getElementById("dg-dev-types").innerHTML =
      Object.entries(lan.summary || {}).sort((a, b) => b[1] - a[1])
        .map(([t, c]) => `<span class="chip">${escHtml(t)} <b>${escHtml(c)}</b></span>`).join("");
    // 掃描範圍的警告（例如用戶隔離）直接顯示在表格上方，避免誤以為設備只有這幾台
    const scope = ((lan.assessment || {}).issues || []).find(i => i.category === "掃描範圍");
    const note = document.getElementById("dg-dev-note");
    note.classList.toggle("hidden", !scope);
    if (scope) note.textContent = `${scope.title}。${scope.recommendation}`;
    const sub = t => t ? `<div class="lan-sub">${t}</div>` : "";
    document.getElementById("dg-dev-tbody").innerHTML = lan.hosts.map(h => `
      <tr><td class="lan-name">${escHtml(h.name || "—")}${h.is_gateway ? ' <span class="lan-gw-dot" title="預設閘道">⚑</span>' : ""}</td>
        <td>${escHtml(h.ip)}</td>
        <td>${escHtml(h.vendor || "未知")}${sub(escHtml(h.mac || ""))}</td>
        <td><span class="lan-type-badge${String(h.type).startsWith("本機") ? " self" : ""}">${escHtml(h.type || "不明")}</span></td>
        <td class="lan-model">${escHtml(h.model || "—")}${sub(escHtml(h.os || (h.os_hint ? h.os_hint + "（推測）" : "")))}</td></tr>`).join("");
  }
  // ── 這個 Wi-Fi 的基地台：與目前連線同 SSID 的所有無線電（一台 AP 通常有 2～3 個）──
  function ownAPs() {
    const cur = _data.current || {};
    if (!cur.connected || !cur.ssid || String(cur.ssid).startsWith("<")) return [];
    return (_data.scan || []).filter(n => n.ssid === cur.ssid)
      .sort((a, b) => (b.rssi ?? -999) - (a.rssi ?? -999));
  }

  function renderAPs() {
    const sec = document.getElementById("dg-ap-section");
    const aps = ownAPs();
    if (!aps.length) { sec.classList.add("hidden"); return; }
    sec.classList.remove("hidden");
    const cur = _data.current || {};
    document.getElementById("dg-ap-count").textContent = aps.length;
    const bands = {};
    aps.forEach(n => { bands[n.band] = (bands[n.band] || 0) + 1; });
    document.getElementById("dg-ap-hint").textContent =
      `　SSID「${cur.ssid}」在這個位置收得到 ${aps.length} 個無線電（` +
      Object.entries(bands).map(([b, c]) => `${b} ${c} 個`).join("、") + "）";
    document.getElementById("dg-ap-tbody").innerHTML = aps.map(n => {
      const mine = n.bssid && n.bssid === cur.bssid;
      return `<tr><td style="font-family:monospace">${escHtml(n.bssid || "—")}</td>
        <td>${escHtml(n.vendor || "—")}</td><td>${escHtml(n.band || "")}</td>
        <td>${escHtml(n.channel ?? "")}</td><td>${escHtml(n.width || "")}</td>
        <td style="color:${rssiToColor(n.rssi)};font-weight:600">${escHtml(n.rssi ?? "—")} dBm</td>
        <td>${escHtml(String(n.standard || "").replace(/\s*\(.*?\)\s*/g, ""))}</td>
        <td>${mine ? '<span class="lan-type-badge self">目前連線</span>' : ""}</td></tr>`;
    }).join("");
  }

  document.getElementById("dg-dev-more").addEventListener("click", e => {
    e.preventDefault();
    document.querySelector('.nav-item[data-tab="lan"]')?.click();
  });

  // ── 頻道佔用長條圖（綠框 = 建議頻道）──────────────────────────────────────
  function drawChannelChart() {
    const canvas = document.getElementById("dg-chchart");
    const wrap   = canvas.parentElement;
    const dpr    = window.devicePixelRatio || 1;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const nets = _data.scan || [];
    const ana  = _data.analysis || {};
    const curCh   = (_data.current || {}).channel;
    const curBand = (_data.current || {}).band;

    const CH24 = [1,2,3,4,5,6,7,8,9,10,11,12,13];
    const CH5  = [36,40,44,48,52,56,60,64,100,108,116,124,132,140,144,149,153,157,161,165];
    const groups = [
      { band: "2.4GHz", chans: CH24, x0: 0.02, x1: 0.40 },
      { band: "5GHz",   chans: CH5,  x0: 0.45, x1: 1.00 },
    ];

    const counts = {};
    nets.forEach(n => {
      const k = `${n.band}|${n.channel}`;
      counts[k] = (counts[k] || 0) + 1;
    });
    const maxCnt = Math.max(2, ...Object.values(counts));
    const baseY  = H - 30;
    const barMaxH = H - 52;

    groups.forEach(g => {
      const best = new Set(((ana[g.band] || {}).best_channels || [])
                           .slice(0, 3).map(b => b.channel));
      const gx0 = g.x0 * W, gx1 = g.x1 * W;
      const bw  = (gx1 - gx0) / g.chans.length;

      ctx.fillStyle = "rgba(255,255,255,.35)";
      ctx.font = `bold ${fs(11)}px -apple-system`;
      ctx.textAlign = "left";
      ctx.fillText(g.band, gx0, 11);

      g.chans.forEach((ch, i) => {
        const cnt = counts[`${g.band}|${ch}`] || 0;
        const x = gx0 + i * bw + 1.5;
        const w = bw - 3;
        const h = cnt ? Math.max(4, cnt / maxCnt * barMaxH) : 2;
        const y = baseY - h;

        const isCur  = g.band === curBand && ch === curCh;
        const color  = !cnt ? "rgba(255,255,255,.10)"
                     : cnt >= 4 ? "#ef4444" : cnt >= 2 ? "#eab308" : "#3b82f6";
        ctx.fillStyle = color;
        ctx.fillRect(x, y, w, h);

        if (best.has(ch)) {                 // 建議乾淨頻道：綠框
          ctx.strokeStyle = "#22c55e";
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x - .5, 16, w + 1, baseY - 16);
        }
        if (isCur) {                        // 目前頻道：白色三角標記
          ctx.fillStyle = "#fff";
          ctx.beginPath();
          ctx.moveTo(x + w/2, y - 9); ctx.lineTo(x + w/2 - 4, y - 3);
          ctx.lineTo(x + w/2 + 4, y - 3); ctx.closePath(); ctx.fill();
        }
        if (cnt) {
          ctx.fillStyle = "rgba(255,255,255,.75)";
          ctx.font = `${fs(9.5)}px -apple-system`;
          ctx.textAlign = "center";
          ctx.fillText(cnt, x + w/2, y - (isCur ? 12 : 3));
        }
        ctx.fillStyle = "rgba(255,255,255,.4)";
        ctx.font = `${fs(9.5)}px -apple-system`;
        ctx.textAlign = "center";
        ctx.fillText(ch, x + w/2, baseY + 11);
      });
    });

    // Legend
    ctx.font = `${fs(11)}px -apple-system`; ctx.textAlign = "left";
    const legend = [["#3b82f6","1 AP"],["#eab308","2-3 AP"],["#ef4444","≥4 AP 壅塞"],["#fff","▲ 目前頻道"]];
    let lx = W - 240;
    legend.forEach(([c, t]) => {
      ctx.fillStyle = c; ctx.fillRect(lx, H - 12, 8, 8);
      ctx.fillStyle = "rgba(255,255,255,.5)";
      ctx.fillText(t, lx + 11, H - 5);
      lx += ctx.measureText(t).width + 28;
    });
  }

  // ── AI 建議清單 ─────────────────────────────────────────────────────────────
  function renderAI() {
    const ai = _data.ai;
    const summaryEl = document.getElementById("dg-ai-summary");
    const listEl    = document.getElementById("dg-ai-list");

    if (!ai || !ai.ok) {
      summaryEl.innerHTML = `<span style="color:#ef4444">AI 分析未完成</span>（其餘測試結果仍有效）`;
      listEl.innerHTML = "";
      return;
    }

    const providerTxt = ai.provider === "rules"
      ? "📋 離線規則引擎"
      : `🤖 由 ${escHtml(ai.provider_label || ai.provider)} 分析`;
    summaryEl.innerHTML =
      `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
         <div><strong>AI 診斷摘要</strong>　${escHtml(ai.summary || "")}</div>
         <div style="font-size: calc(12.5px * var(--fs, 1));color:var(--text-3);white-space:nowrap">${providerTxt} ｜ 健康分數 <strong style="color:${ai.score>=70?"#22c55e":ai.score>=50?"#eab308":"#ef4444"}">${escHtml(ai.score)}</strong>/100</div>
       </div>`;

    const PRIO = [
      { key: "immediate", title: "🔴 立即處理", cls: "critical", badge: "sev-critical", badgeTx: "立即" },
      { key: "adjust",    title: "🟡 建議調整", cls: "warning",  badge: "sev-warning",  badgeTx: "調整" },
      { key: "ok",        title: "🟢 狀態正常", cls: "good",     badge: "sev-good",     badgeTx: "正常" },
    ];

    let html = "";
    PRIO.forEach(p => {
      const group = (ai.items || []).filter(i => i.priority === p.key);
      if (!group.length) return;
      html += `<div class="dg-prio-title">${p.title}（${group.length}）</div>`;
      html += group.map(i => `
        <div class="diag-issue ${p.cls}">
          <div class="diag-issue-icon">${p.key === "immediate" ? "🔴" : p.key === "adjust" ? "🟡" : "🟢"}</div>
          <div class="diag-issue-body">
            <div class="diag-issue-title">${escHtml(i.problem)}</div>
            <div class="diag-issue-rec">→ ${escHtml(i.action)}</div>
          </div>
          <div>
            <span class="diag-sev-badge ${p.badge}">${p.badgeTx}</span>
            <div style="font-size: calc(12px * var(--fs, 1));color:var(--text-3);margin-top:3px;text-align:center">${escHtml(i.category || "")}</div>
          </div>
        </div>`).join("");
    });
    listEl.innerHTML = html;
  }

  // 顯示 AI provider 提示（idle 畫面）；本分頁是預設分頁，載入時即可見 → 立即執行，
  // 若非可見分頁則延到第一次開啟
  const showEngine = () => fetch("/api/info").then(r => r.json()).then(d => {
    document.getElementById("dg-ai-provider").textContent =
      d.ai_provider === "rules"
        ? "📋 AI 分析引擎：離線規則引擎（可到「設定」選擇大語言模型並填入 API 金鑰）"
        : `🤖 AI 分析引擎：${d.ai_provider_label}`;
  }).catch(() => {});
  onTabFirstActive("diagnose", showEngine);
  window.addEventListener("settings:saved", showEngine);   // 設定頁改了引擎 → 立即更新提示
})();
