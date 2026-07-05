/* ═══════════════════════════════════════════════════════════════════════════
   diagnose.js — 現場診斷首頁（一鍵測試 + AI 建議行動）
   EventSource → /api/diagnose/stream，依事件更新進度，完成後渲染：
   上：數值儀表卡片 ＋ 頻道佔用長條圖
   下：AI 建議行動清單（🔴 立即處理 / 🟡 建議調整 / 🟢 狀態正常）
   ═══════════════════════════════════════════════════════════════════════════ */

(() => {
  const DG_STEPS = [
    { key: "scan",      label: "Wi-Fi 環境掃描" },
    { key: "analysis",  label: "頻道分析" },
    { key: "current",   label: "目前連線狀態" },
    { key: "netperf",   label: "Ping / DNS 延遲測試" },
    { key: "speedtest", label: "網路速度測試（上傳/下載）" },
    { key: "stability", label: "連線穩定度測試" },
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
      const url  = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
      a.href = url; a.download = `wifi-diagnosis-${ts}.pdf`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    renderSteps();

    _es = new EventSource(`/api/diagnose/stream?duration=${duration}&speedtest=1&ai=1`);

    _es.addEventListener("step", e => {
      const d  = JSON.parse(e.data);
      const el = _stepEls[d.key];
      if (!el) return;
      if (d.status === "running") {
        el.className = "diag-step running";
        el.innerHTML = `<span class="step-icon"></span>${d.label}`;
      } else if (d.status === "ok") {
        el.className = "diag-step ok";
        el.innerHTML = `<span class="step-icon"></span>${d.label}`;
      } else {
        el.className = "diag-step fail";
        el.innerHTML = `<span class="step-icon"></span>${d.label}` +
          (d.error ? ` <span style="font-size:10px;color:var(--text-3)">（${escHtml(d.error)}）</span>` : "");
      }
    });

    _es.addEventListener("progress", e => {
      const d  = JSON.parse(e.data);
      const el = _stepEls[d.key];
      if (!el || !el.classList.contains("running")) return;
      const extra = d.key === "stability"
        ? `（${Math.round(d.elapsed)} / ${d.total} 秒）`
        : `（已分析 ${d.elapsed} 秒）`;
      const lbl = DG_STEPS.find(s => s.key === d.key)?.label || "";
      el.innerHTML = `<span class="step-icon"></span>${lbl} <span style="font-size:10px;color:var(--text-3)">${extra}</span>`;
    });

    _es.addEventListener("partial", e => {
      const d = JSON.parse(e.data);
      _data[d.key] = d.data;
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
    btn.querySelector(".dg-bigbtn-label").textContent = "開始診斷";
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

    const card = (label, val, sub = "", color = "") => `
      <div class="card"><div class="card-label">${label}</div>
        <div class="card-val" ${color ? `style="color:${color}"` : ""}>${val}</div>
        ${sub ? `<div class="card-sub">${sub}</div>` : ""}</div>`;

    const fmt = (v, unit) => v != null ? `${v}<span style="font-size:11px;color:var(--text-2)"> ${unit}</span>` : "—";

    document.getElementById("dg-cards").innerHTML = [
      card("訊號 RSSI", cur.connected ? fmt(cur.rssi, "dBm") : "未連線",
           cur.ssid || "", cur.connected ? rssiToColor(cur.rssi) : "#ef4444"),
      card("SNR", fmt(cur.snr, "dB"), "", cur.snr != null ? snrColor(cur.snr) : ""),
      card("下載", fmt(spd.down_mbps, "Mbps")),
      card("上傳", fmt(spd.up_mbps, "Mbps")),
      card("Ping", fmt(np.ping_ms, "ms"), np.ping_jitter_ms != null ? `Jitter ${np.ping_jitter_ms} ms` : ""),
      card("掉包", fmt(np.ping_loss_pct, "%"),
           "", np.ping_loss_pct > 0 ? "#ef4444" : "#22c55e"),
      card("穩定度", stab.score != null
           ? `${stab.score}<span style="font-size:11px;color:var(--text-2)"> /100</span>` : "—",
           stab.grade?.label || "", stab.grade?.color || ""),
    ].join("");
  }

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
      ctx.font = "bold 9px -apple-system";
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
          ctx.font = "8px -apple-system";
          ctx.textAlign = "center";
          ctx.fillText(cnt, x + w/2, y - (isCur ? 12 : 3));
        }
        ctx.fillStyle = "rgba(255,255,255,.4)";
        ctx.font = "8px -apple-system";
        ctx.textAlign = "center";
        ctx.fillText(ch, x + w/2, baseY + 11);
      });
    });

    // Legend
    ctx.font = "9px -apple-system"; ctx.textAlign = "left";
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

    const providerTxt = ai.provider === "codex"
      ? "🤖 由 Codex AI 分析"
      : "📋 離線規則引擎（未偵測到 Codex CLI）";
    summaryEl.innerHTML =
      `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
         <div><strong>AI 診斷摘要</strong>　${escHtml(ai.summary || "")}</div>
         <div style="font-size:10.5px;color:var(--text-3);white-space:nowrap">${providerTxt} ｜ 健康分數 <strong style="color:${ai.score>=70?"#22c55e":ai.score>=50?"#eab308":"#ef4444"}">${ai.score}</strong>/100</div>
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
            <div style="font-size:10px;color:var(--text-3);margin-top:3px;text-align:center">${escHtml(i.category || "")}</div>
          </div>
        </div>`).join("");
    });
    listEl.innerHTML = html;
  }

  // 顯示 AI provider 提示（idle 畫面）
  fetch("/api/info").then(r => r.json()).then(d => {
    document.getElementById("dg-ai-provider").textContent =
      d.ai_provider === "codex"
        ? "🤖 AI 分析引擎：Codex CLI（已登入）"
        : "📋 AI 分析引擎：離線規則引擎（安裝並登入 Codex CLI 可啟用 AI 分析）";
  }).catch(() => {});
})();
