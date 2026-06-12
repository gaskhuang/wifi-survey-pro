/* ═══════════════════════════════════════════════════════════════════════════
   spectrumtab.js — 頻譜分析儀分頁
   · 單頻段放大即時頻譜（Y 軸 = dBm），bell-curve 每 AP 一條
   · 時間瀑布圖（waterfall）：新掃描在最上方，顏色 = 訊號強度
   · 頻段切換 2.4 / 5 / 6 GHz、掃描間隔、凍結
   只在分頁可見時輪詢 /api/scan，避免背景浪費。
   ═══════════════════════════════════════════════════════════════════════════ */

const SpectrumTab = (() => {

  const BAND_FREQ = {
    "2.4GHz": [2400, 2495],
    "5GHz":   [5150, 5850],
    "6GHz":   [5925, 7130],
  };
  const TICKS = {
    "2.4GHz": [1,2,3,4,5,6,7,8,9,10,11,12,13],
    "5GHz":   [36,40,44,48,52,56,60,64,100,108,116,124,132,140,144,149,153,157,161,165],
    "6GHz":   [1,17,33,49,65,81,97,113,129,145,161,177,193,213,233],
  };
  const N_BINS   = 600;   // waterfall frequency resolution
  const MAX_ROWS = 160;   // waterfall history depth
  const ML = 44, MR = 14, MT = 10, MB = 24;          // live chart margins
  const DB_TOP = -20, DB_BOT = -100;

  let _band     = "2.4GHz";
  let _frozen   = false;
  let _sweeps   = 0;
  let _rows     = [];     // newest first: {bins: Float32Array}
  let _lastNets = [];
  let _curBssid = "";
  let _timer    = null;

  // ── Helpers ────────────────────────────────────────────────────────────────
  const chFreq = (ch, band) =>
    band === "2.4GHz" ? 2407 + ch * 5 :
    band === "5GHz"   ? 5000 + ch * 5 : 5950 + ch * 5;

  // 5 GHz 真實頻道區塊（與 spectrum.js 相同的查表法），回傳 [fLeft, fRight] MHz
  function chBlockFreq(ch, widthMHz, band) {
    if (band === "5GHz" && widthMHz >= 40) {
      const tables = {
        40:  [[36,44],[44,52],[52,60],[60,68],[100,108],[108,116],[116,124],
              [124,132],[132,140],[140,148],[149,157],[157,165],[165,173],[173,181]],
        80:  [[36,52],[52,68],[100,116],[116,132],[132,148],[149,165],[165,181]],
        160: [[36,68],[100,132],[149,181]],
      };
      for (const [l, r] of (tables[widthMHz] || [])) {
        if (ch >= l && ch < r) return [chFreq(l, band) - 10, chFreq(r, band) - 10];
      }
    }
    const c = chFreq(ch, band);
    return [c - widthMHz / 2, c + widthMHz / 2];
  }

  const rssiNorm = rssi => Math.max(0, Math.min(1, (rssi + 100) / 80));

  // ── Waterfall colormap: black → blue → cyan → green → yellow → red ────────
  const CMAP = [
    [0.00, [11, 13, 20]], [0.15, [20, 40, 120]], [0.35, [0, 160, 200]],
    [0.55, [30, 200, 90]], [0.75, [240, 220, 50]], [1.00, [255, 60, 40]],
  ];
  function cmap(v) {
    for (let i = 1; i < CMAP.length; i++) {
      if (v <= CMAP[i][0]) {
        const [t0, c0] = CMAP[i-1], [t1, c1] = CMAP[i];
        const u = (v - t0) / (t1 - t0);
        return [0,1,2].map(k => Math.round(c0[k] + (c1[k] - c0[k]) * u));
      }
    }
    return CMAP[CMAP.length - 1][1];
  }

  // ── Scan loop（鏈式 setTimeout，等掃描回來才排下一次）──────────────────────
  function active() {
    return document.getElementById("tab-spectrumtab")?.classList.contains("active");
  }

  async function tick() {
    if (active() && !_frozen) {
      try {
        const [scanR, curR] = await Promise.all([
          fetch("/api/scan").then(r => r.json()),
          fetch("/api/current").then(r => r.json()),
        ]);
        _lastNets = scanR.networks || [];
        _curBssid = curR.connected ? (curR.bssid || "") : "";
        _sweeps++;
        pushRow(_lastNets);
        render();
      } catch (_) {}
    }
    const iv = parseFloat(document.getElementById("spa-interval").value) || 2;
    _timer = setTimeout(tick, iv * 1000);
  }

  // ── Waterfall data ─────────────────────────────────────────────────────────
  function pushRow(nets) {
    const [fMin, fMax] = BAND_FREQ[_band];
    const bins = new Float32Array(N_BINS);
    for (const n of nets) {
      if (n.band !== _band) continue;
      const w = parseInt(n.width) || 20;
      const [fL, fR] = chBlockFreq(n.channel, w, _band);
      const peak = rssiNorm(n.rssi);
      const cF   = (fL + fR) / 2, half = (fR - fL) / 2;
      const bL = Math.max(0, Math.floor((fL - fMin) / (fMax - fMin) * N_BINS));
      const bR = Math.min(N_BINS - 1, Math.ceil((fR - fMin) / (fMax - fMin) * N_BINS));
      for (let b = bL; b <= bR; b++) {
        const f = fMin + (b + 0.5) / N_BINS * (fMax - fMin);
        const u = Math.abs(f - cF) / half;                  // 0 at peak → 1 at edge
        const v = peak * 0.5 * (1 + Math.cos(Math.PI * Math.min(1, u)));
        if (v > bins[b]) bins[b] = v;
      }
    }
    _rows.unshift({ bins });
    if (_rows.length > MAX_ROWS) _rows.length = MAX_ROWS;
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  function render() {
    drawLive();
    drawWaterfall();
    updateCards();
  }

  function setupCanvas(id) {
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const wrap = canvas.parentElement;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    if (!W || !H) return null;
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width  = W + "px";
    canvas.style.height = H + "px";
    canvas.width  = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    return { ctx, W, H };
  }

  function freqToX(f, W) {
    const [fMin, fMax] = BAND_FREQ[_band];
    return ML + (f - fMin) / (fMax - fMin) * (W - ML - MR);
  }
  const dbToY = (db, H) =>
    MT + (DB_TOP - db) / (DB_TOP - DB_BOT) * (H - MT - MB);

  function drawLive() {
    const c = setupCanvas("spa-live-canvas");
    if (!c) return;
    const { ctx, W, H } = c;

    ctx.fillStyle = "#0b0d14";
    ctx.fillRect(0, 0, W, H);

    // Grid + dBm labels
    ctx.font = "9px -apple-system";
    for (let db = DB_TOP; db >= DB_BOT; db -= 10) {
      const y = dbToY(db, H);
      ctx.strokeStyle = "rgba(255,255,255,.06)";
      ctx.setLineDash([3, 6]);
      ctx.beginPath(); ctx.moveTo(ML, y); ctx.lineTo(W - MR, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(255,255,255,.3)";
      ctx.textAlign = "right";
      ctx.fillText(db, ML - 6, y + 3);
    }

    // Channel ticks
    ctx.textAlign = "center";
    for (const ch of TICKS[_band]) {
      const x = freqToX(chFreq(ch, _band), W);
      if (x < ML || x > W - MR) continue;
      ctx.strokeStyle = "rgba(255,255,255,.12)";
      ctx.beginPath(); ctx.moveTo(x, H - MB); ctx.lineTo(x, H - MB + 4); ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,.35)";
      ctx.fillText(ch, x, H - MB + 14);
    }

    // AP curves（弱訊號先畫）
    const nets = _lastNets.filter(n => n.band === _band)
                          .sort((a, b) => a.rssi - b.rssi);
    for (const n of nets) {
      const w = parseInt(n.width) || 20;
      const [fL, fR] = chBlockFreq(n.channel, w, _band);
      const xL = Math.max(ML, freqToX(fL, W));
      const xR = Math.min(W - MR, freqToX(fR, W));
      const xP = Math.max(xL, Math.min(xR, freqToX((fL + fR) / 2, W)));
      if (xR - xL <= 1) continue;
      const yTop = dbToY(Math.max(DB_BOT, Math.min(DB_TOP, n.rssi)), H);
      const yBot = H - MB;
      const color  = n.net_color || "#3b82f6";
      const isConn = n.bssid === _curBssid;

      ctx.beginPath();
      ctx.moveTo(xL, yBot);
      ctx.bezierCurveTo(xL + (xP-xL)*0.4, yBot, xL + (xP-xL)*0.6, yTop, xP, yTop);
      ctx.bezierCurveTo(xP + (xR-xP)*0.4, yTop, xP + (xR-xP)*0.6, yBot, xR, yBot);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, yTop, 0, yBot);
      grad.addColorStop(0, color + (isConn ? "cc" : "55"));
      grad.addColorStop(1, color + "14");
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.strokeStyle = color + (isConn ? "ff" : "88");
      ctx.lineWidth   = isConn ? 2.5 : 1;
      ctx.stroke();

      // Label
      const ssid = (n.ssid && !n.ssid.startsWith("<")) ? n.ssid : "(隱藏)";
      const label = `ch${n.channel} ${ssid.length > 16 ? ssid.slice(0, 15) + "…" : ssid}`;
      ctx.font = `${isConn ? "700 11px" : "500 9px"} -apple-system`;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = "#0b0d14cc";
      ctx.fillRect(xP - tw/2 - 4, yTop - 16, tw + 8, 13);
      ctx.fillStyle = isConn ? "#fff" : color + "ee";
      ctx.textAlign = "center";
      ctx.fillText(label, xP, yTop - 6);
    }

    if (!nets.length) {
      ctx.fillStyle = "rgba(255,255,255,.25)";
      ctx.font = "12px -apple-system";
      ctx.textAlign = "center";
      ctx.fillText(_sweeps ? `此頻段（${_band}）目前無 AP` : "掃描中…", W / 2, H / 2);
    }
  }

  function drawWaterfall() {
    const c = setupCanvas("spa-waterfall-canvas");
    if (!c) return;
    const { ctx, W, H } = c;
    const dpr = window.devicePixelRatio || 1;

    ctx.fillStyle = "#0b0d14";
    ctx.fillRect(0, 0, W, H);
    if (!_rows.length) return;

    const plotW = Math.max(10, Math.round(W - ML - MR));
    const plotH = Math.max(10, Math.round(H - MB));
    const img   = ctx.createImageData(plotW, plotH);
    // 每列 ≥4px：約 60 列就能填滿視窗（2 秒/掃 ≈ 2 分鐘歷史），不用等 MAX_ROWS
    const rowPx = Math.max(4, Math.floor(plotH / 60));

    for (let y = 0; y < plotH; y++) {
      const row = _rows[Math.floor(y / rowPx)];
      for (let x = 0; x < plotW; x++) {
        let rgb;
        if (row) {
          const v = row.bins[Math.floor(x / plotW * N_BINS)];
          rgb = cmap(v);
        } else {
          rgb = [11, 13, 20];
        }
        const i = (y * plotW + x) * 4;
        img.data[i] = rgb[0]; img.data[i+1] = rgb[1];
        img.data[i+2] = rgb[2]; img.data[i+3] = 255;
      }
    }
    // putImageData ignores transforms — paint via offscreen canvas so DPR scaling applies
    const off = document.createElement("canvas");
    off.width = plotW; off.height = plotH;
    off.getContext("2d").putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ML, 0, plotW, plotH);

    // Channel ticks
    ctx.font = "9px -apple-system";
    ctx.textAlign = "center";
    for (const ch of TICKS[_band]) {
      const x = freqToX(chFreq(ch, _band), W);
      if (x < ML || x > W - MR) continue;
      ctx.strokeStyle = "rgba(255,255,255,.12)";
      ctx.beginPath(); ctx.moveTo(x, plotH); ctx.lineTo(x, plotH + 4); ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,.35)";
      ctx.fillText(ch, x, plotH + 14);
    }
    // Time axis hint
    ctx.save();
    ctx.translate(12, plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "rgba(255,255,255,.3)";
    ctx.textAlign = "center";
    ctx.fillText("← 時間（新 → 舊）", 0, 0);
    ctx.restore();
  }

  function updateCards() {
    const nets = _lastNets.filter(n => n.band === _band);
    document.getElementById("spa-count").textContent  = nets.length;
    document.getElementById("spa-sweeps").textContent = _sweeps;

    if (nets.length) {
      const top = nets.reduce((a, b) => (a.rssi > b.rssi ? a : b));
      const ssid = (top.ssid && !top.ssid.startsWith("<")) ? top.ssid : "(隱藏)";
      document.getElementById("spa-strongest").innerHTML =
        `${top.rssi} <span style="font-size:11px;color:var(--text-2)">dBm · ${escHtml(ssid)}</span>`;

      const byCh = {};
      nets.forEach(n => { byCh[n.channel] = (byCh[n.channel] || 0) + 1; });
      const busiest = Object.entries(byCh).sort((a, b) => b[1] - a[1])[0];
      document.getElementById("spa-busiest").innerHTML =
        `ch${busiest[0]} <span style="font-size:11px;color:var(--text-2)">${busiest[1]} AP</span>`;
    } else {
      document.getElementById("spa-strongest").textContent = "—";
      document.getElementById("spa-busiest").textContent   = "—";
    }
  }

  // ── Controls ───────────────────────────────────────────────────────────────
  document.querySelectorAll("#spa-bands .iperf-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#spa-bands .iperf-mode-btn")
        .forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _band = btn.dataset.band;
      _rows = [];          // 不同頻段的頻率軸不同，瀑布圖重置
      render();
    });
  });

  document.getElementById("spa-freeze").addEventListener("click", e => {
    _frozen = !_frozen;
    const btn = e.currentTarget;
    btn.innerHTML = _frozen
      ? `<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>恢復`
      : `<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>暫停`;
    btn.classList.toggle("btn-success", _frozen);
  });

  // 切到本分頁時立即重繪（canvas 在隱藏時量不到尺寸）
  document.querySelector('.nav-item[data-tab="spectrumtab"]')
    ?.addEventListener("click", () => setTimeout(render, 50));
  window.addEventListener("resize", () => { if (active()) render(); });

  tick();   // start loop（非 active 時只是空轉計時器）

  return { render };
})();
