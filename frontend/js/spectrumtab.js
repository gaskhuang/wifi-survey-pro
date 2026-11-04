/* ═══════════════════════════════════════════════════════════════════════════
   spectrumtab.js — 頻譜分析儀分頁
   · 單頻段放大即時頻譜（Y 軸 = dBm），bell-curve 每 AP 一條
   · 時間瀑布圖（waterfall）：2D 熱力圖 / 3D 堆疊立體圖（可拖曳旋轉視角）
   · Max Hold（峰值保持）、移動平均基線、Peak Search 峰值標記
   · 錄製 / 儲存 / 載入 / 回放（仿 NXT-2000 .nxt sweep buffer）
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
  const ROWS_3D  = 70;    // 3D 立體瀑布最大堆疊列數
  const MAX_REC  = 3000;  // 錄製緩衝最大 sweep 數
  // 錄製只保留回放 / 建議需要的欄位（原本存整個 network 物件）。記憶體上限約：
  // 3000 sweep × N 個 AP × 9 欄位，外加每 sweep 一份 bins 快取 600×4B ≈ 7.2 MB
  const REC_FIELDS = ["band", "channel", "width", "rssi", "snr", "ssid", "bssid", "net_color", "standard"];
  const SCAN_TIMEOUT_MS = 20000;   // 單次 /api/scan 逾時，避免卡住鏈式輪詢
  const AVG_A    = 0.2;   // 移動平均 EMA 係數
  const ML = 44, MR = 14, MT = 10, MB = 24;          // live chart margins
  const DB_TOP = -20, DB_BOT = -100;

  let _band     = "2.4GHz";
  let _frozen   = false;
  let _sweeps   = 0;
  let _rows     = [];     // newest first: {bins: Float32Array}
  let _lastNets = [];
  let _curBssid = "";
  let _timer    = null;

  // ── 疊加分析狀態 ────────────────────────────────────────────────────────────
  let _maxHold    = new Float32Array(N_BINS);
  let _avgBins    = new Float32Array(N_BINS);
  let _avgInit    = false;
  let _showMaxHold = true;
  let _showAvg     = false;
  let _showPeak    = true;

  // ── 瀑布視圖 ────────────────────────────────────────────────────────────────
  let _wfMode = "2d";                              // "2d" | "3d"
  const _d3   = { skewX: 0.42, depthY: 0.60, amp: 0.85 };   // 3D 視角參數
  let _wf3dBound = false;

  // ── 錄製 / 回放 ─────────────────────────────────────────────────────────────
  let _mode      = "live";   // "live" | "playback"
  let _recording = false;
  let _recBuf    = [];       // [{t, nets}]
  let _playing   = false;
  let _playIdx   = 0;
  let _playTimer = null;
  let _playSuspended = false;  // 分頁 / 視窗隱藏時暫停回放計時器
  // 回放增量狀態：maxHold/avg/rows 已累積到第幾個 sweep（-1 = 需整段重建）
  let _pbAccIdx  = -1;
  let _pbAccBand = "";
  const _binCache = new WeakMap();   // sweep 物件 → {band, bins}，避免重複 computeBins

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
  const normToDb = v    => v * 80 - 100;                  // rssiNorm 的反函數

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
  const rgbStr = (rgb, a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;

  // 瀑布圖用的預先計算色表（packed RGBA，直接寫入 Uint32 view）。
  // 4096 階：與逐點 cmap() 相比每通道最大誤差 1（256 階會到 3）。
  const LUT_N = 4096;
  const LITTLE_ENDIAN = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
  const packRGBA = (r, g, b) => (LITTLE_ENDIAN
    ? ((255 << 24) | (b << 16) | (g << 8) | r)
    : ((r << 24) | (g << 16) | (b << 8) | 255)) >>> 0;
  const CMAP_LUT = new Uint32Array(LUT_N);
  for (let i = 0; i < LUT_N; i++) {
    const c = cmap(i / (LUT_N - 1));
    CMAP_LUT[i] = packRGBA(c[0], c[1], c[2]);
  }
  const WF_BG = packRGBA(11, 13, 20);

  // ── Scan loop（鏈式 setTimeout，等掃描回來才排下一次）──────────────────────
  function active() {
    return document.getElementById("tab-spectrumtab")?.classList.contains("active");
  }

  async function tick() {
    if (active() && !document.hidden && !_frozen && _mode === "live") {
      try {
        // 已連線 BSSID 直接用 app.js 輪詢發布的 APP.current；還沒有才並行抓一次
        const curP = APP.current ? Promise.resolve(APP.current)
                                 : getCurrentConn().catch(() => null);
        const [scanR, curR] = await Promise.all([
          fetchJSON("/api/scan", {}, SCAN_TIMEOUT_MS),
          curP,
        ]);
        _lastNets = scanR.networks || [];
        _curBssid = curR?.connected ? (curR.bssid || "") : "";
        _sweeps++;
        const bins = pushSweep(_lastNets);
        if (_recording) {
          const sw = { t: Date.now(), nets: slimNets(_lastNets) };
          _binCache.set(sw, { band: _band, bins });
          _recBuf.push(sw);
          if (_recBuf.length > MAX_REC) _recBuf.shift();
          updateRecLabel();
        }
        render();
      } catch (_) {}
    }
    const iv = parseFloat(document.getElementById("spa-interval").value) || 2;
    _timer = setTimeout(tick, iv * 1000);
  }

  function slimNets(nets) {
    return nets.map(n => {
      const o = {};
      for (const k of REC_FIELDS) if (n[k] !== undefined) o[k] = n[k];
      return o;
    });
  }

  // ── Waterfall / 疊加資料 ─────────────────────────────────────────────────────
  function computeBins(nets) {
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
    return bins;
  }

  function pushSweep(nets) {
    const bins = computeBins(nets);
    for (let b = 0; b < N_BINS; b++) {
      if (bins[b] > _maxHold[b]) _maxHold[b] = bins[b];
      _avgBins[b] = _avgInit ? _avgBins[b] * (1 - AVG_A) + bins[b] * AVG_A : bins[b];
    }
    _avgInit = true;
    _rows.unshift({ bins });
    if (_rows.length > MAX_ROWS) _rows.length = MAX_ROWS;
    return bins;
  }

  // 切頻段 / 載入錄製 / 重置時清空所有累積狀態
  function resetSpectrumState() {
    _rows = [];
    _maxHold = new Float32Array(N_BINS);
    _avgBins = new Float32Array(N_BINS);
    _avgInit = false;
    _pbAccIdx = -1;
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  function render() {
    drawLive();
    if (_wfMode === "3d") draw3DWaterfall();
    else                  drawWaterfall();
    updateCards();
  }

  // 只有尺寸 / DPR 改變才重設 canvas.width/height（會重新配置 backing store，
  // DPR 2 時每次約數 MB）；否則沿用並把 context 狀態恢復成預設值再畫。
  function setupCanvas(id) {
    const canvas = document.getElementById(id);
    if (!canvas) return null;
    const wrap = canvas.parentElement;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    if (!W || !H) return null;
    const dpr = window.devicePixelRatio || 1;
    const ctx = canvas.getContext("2d");
    if (canvas._W !== W || canvas._H !== H || canvas._dpr !== dpr) {
      canvas.style.width  = W + "px";
      canvas.style.height = H + "px";
      canvas.width  = W * dpr;
      canvas.height = H * dpr;
      canvas._W = W; canvas._H = H; canvas._dpr = dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    } else {
      resetCtx(ctx, canvas, dpr);
    }
    return { ctx, W, H };
  }

  // 等同重設 canvas 尺寸後的乾淨狀態（各 draw 函式假設從預設狀態開始）
  function resetCtx(ctx, canvas, dpr) {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = "#000000";
    ctx.strokeStyle = "#000000";
    ctx.lineWidth = 1;
    ctx.lineCap = "butt";
    ctx.lineJoin = "miter";
    ctx.miterLimit = 10;
    ctx.setLineDash([]);
    ctx.lineDashOffset = 0;
    ctx.font = "10px sans-serif";
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
    ctx.shadowBlur = 0;
    ctx.shadowColor = "rgba(0, 0, 0, 0)";
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 0;
    ctx.imageSmoothingEnabled = true;
  }

  function freqToX(f, W) {
    const [fMin, fMax] = BAND_FREQ[_band];
    return ML + (f - fMin) / (fMax - fMin) * (W - ML - MR);
  }
  const dbToY = (db, H) =>
    MT + (DB_TOP - db) / (DB_TOP - DB_BOT) * (H - MT - MB);

  // bin 索引 → 該 bin 中心頻率對應的螢幕 X
  function binToX(b, W) {
    const [fMin, fMax] = BAND_FREQ[_band];
    return freqToX(fMin + (b + 0.5) / N_BINS * (fMax - fMin), W);
  }

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

    // ── 疊加：移動平均基線（先畫，墊在最底）──────────────────────────────────
    if (_showAvg && _avgInit) {
      ctx.beginPath();
      let started = false;
      for (let b = 0; b < N_BINS; b++) {
        if (_avgBins[b] <= 0.001) { started = false; continue; }
        const x = binToX(b, W), y = dbToY(normToDb(_avgBins[b]), H);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = "rgba(148,163,184,.85)";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([4, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // ── 疊加：Max Hold 峰值保持線 ───────────────────────────────────────────
    if (_showMaxHold) {
      ctx.beginPath();
      let started = false;
      for (let b = 0; b < N_BINS; b++) {
        if (_maxHold[b] <= 0.001) { started = false; continue; }
        const x = binToX(b, W), y = dbToY(normToDb(_maxHold[b]), H);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = "rgba(251,191,36,.95)";   // amber
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }

    // ── 疊加：Peak Search 峰值標記（本頻段最強 AP）──────────────────────────
    if (_showPeak && nets.length) {
      const top = nets[nets.length - 1];            // nets 已由弱到強排序
      const w = parseInt(top.width) || 20;
      const [fL, fR] = chBlockFreq(top.channel, w, _band);
      const xP = freqToX((fL + fR) / 2, W);
      const yP = dbToY(Math.max(DB_BOT, Math.min(DB_TOP, top.rssi)), H);
      ctx.strokeStyle = "#f43f5e";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(xP, MT); ctx.lineTo(xP, H - MB);
      ctx.moveTo(ML, yP); ctx.lineTo(W - MR, yP);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(xP, yP, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#f43f5e";
      ctx.fill();
      const ssid = (top.ssid && !top.ssid.startsWith("<")) ? top.ssid : "(隱藏)";
      const txt = `▲ 峰值 ${top.rssi} dBm · ch${top.channel} · ${ssid}`;
      ctx.font = "600 10px -apple-system";
      ctx.textAlign = xP > W / 2 ? "right" : "left";
      const tx = xP > W / 2 ? xP - 8 : xP + 8;
      const tw = ctx.measureText(txt).width;
      ctx.fillStyle = "#0b0d14d0";
      ctx.fillRect(ctx.textAlign === "right" ? tx - tw - 4 : tx - 4, yP - 22, tw + 8, 14);
      ctx.fillStyle = "#fda4af";
      ctx.fillText(txt, tx, yP - 12);
    }

    // 圖例
    if (_showMaxHold || _showAvg) {
      ctx.font = "9px -apple-system";
      ctx.textAlign = "left";
      let lx = ML + 4;
      if (_showMaxHold) {
        ctx.fillStyle = "rgba(251,191,36,.95)"; ctx.fillRect(lx, MT + 2, 14, 2);
        ctx.fillText("Max Hold", lx + 18, MT + 6); lx += 78;
      }
      if (_showAvg) {
        ctx.fillStyle = "rgba(148,163,184,.85)"; ctx.fillRect(lx, MT + 2, 14, 2);
        ctx.fillText("平均", lx + 18, MT + 6);
      }
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

    ctx.fillStyle = "#0b0d14";
    ctx.fillRect(0, 0, W, H);
    if (!_rows.length) return;

    const plotW = Math.max(10, Math.round(W - ML - MR));
    const plotH = Math.max(10, Math.round(H - MB));
    const wf = getWfBuffers(plotW, plotH);
    fillWaterfall(wf.px, wf.xb, plotW, plotH, _rows);
    // putImageData ignores transforms — paint via offscreen canvas so DPR scaling applies
    wf.offCtx.putImageData(wf.img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(wf.off, ML, 0, plotW, plotH);

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

  // 瀑布像素緩衝：ImageData / 離屏 canvas / x→bin 對照表，尺寸不變就重複使用
  let _wf = null;
  function getWfBuffers(plotW, plotH) {
    if (_wf && _wf.w === plotW && _wf.h === plotH) return _wf;
    const off = document.createElement("canvas");
    off.width = plotW; off.height = plotH;
    const offCtx = off.getContext("2d");
    const img = offCtx.createImageData(plotW, plotH);
    const xb = new Uint16Array(plotW);
    for (let x = 0; x < plotW; x++) xb[x] = Math.floor(x / plotW * N_BINS);
    _wf = { w: plotW, h: plotH, off, offCtx, img, xb, px: new Uint32Array(img.data.buffer) };
    return _wf;
  }

  // 每個瀑布列只算一條掃描線（查色表），其餘 rowPx-1 條直接 copyWithin 複製
  function fillWaterfall(px, xb, plotW, plotH, rows) {
    // 每列 ≥4px：約 60 列就能填滿視窗（2 秒/掃 ≈ 2 分鐘歷史），不用等 MAX_ROWS
    const rowPx = Math.max(4, Math.floor(plotH / 60));
    const LMAX = LUT_N - 1;
    for (let y0 = 0, r = 0; y0 < plotH; y0 += rowPx, r++) {
      const row  = rows[r];
      const base = y0 * plotW;
      if (row) {
        const bins = row.bins;
        for (let x = 0; x < plotW; x++) px[base + x] = CMAP_LUT[(bins[xb[x]] * LMAX + 0.5) | 0];
      } else {
        px.fill(WF_BG, base, base + plotW);
      }
      const yEnd = Math.min(plotH, y0 + rowPx);
      for (let y = y0 + 1; y < yEnd; y++) px.copyWithin(y * plotW, base, base + plotW);
    }
  }

  // ── 3D 立體瀑布（純 canvas 斜投影堆疊脊線，可拖曳旋轉視角）──────────────────
  function draw3DWaterfall() {
    const c = setupCanvas("spa-waterfall-canvas");
    if (!c) return;
    const { ctx, W, H } = c;
    bind3DInteraction();

    ctx.fillStyle = "#080a11";
    ctx.fillRect(0, 0, W, H);

    const R = Math.min(_rows.length, ROWS_3D);
    if (!R) {
      ctx.fillStyle = "rgba(255,255,255,.25)";
      ctx.font = "12px -apple-system";
      ctx.textAlign = "center";
      ctx.fillText("等待掃描資料…", W / 2, H / 2);
      return;
    }

    const padL = ML, padR = MR + 10, padT = 14, padB = 22;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const shiftXmax = plotW * 0.30 * _d3.skewX;       // 最深一列的水平位移
    const baseW     = plotW - Math.abs(shiftXmax);    // 最前列寬度
    const depthYpx  = plotH * _d3.depthY;             // 由前到後抬升高度
    const ampH      = plotH * 0.34 * _d3.amp;         // 振幅最大像素高
    const frontY    = padT + plotH - 4;               // 最前（最新）一列基線 Y
    const STEP = 3;                                    // bin 取樣步距（效能）

    const originX = dz => padL + Math.max(0, shiftXmax) - shiftXmax * dz;
    const rowW    = dz => baseW;
    const baseYof = dz => frontY - dz * depthYpx;

    // 由後（舊）往前（新）畫，新列覆蓋舊列形成遮擋
    for (let r = R - 1; r >= 0; r--) {
      const dz   = R > 1 ? r / (R - 1) : 0;
      const bins = _rows[r].bins;
      const ox = originX(dz), w = rowW(dz), baseY = baseYof(dz);
      let peak = 0;
      for (let b = 0; b < N_BINS; b++) if (bins[b] > peak) peak = bins[b];
      const rgb = cmap(peak);

      ctx.beginPath();
      ctx.moveTo(ox, baseY);
      for (let b = 0; b < N_BINS; b += STEP) {
        const x = ox + (b / (N_BINS - 1)) * w;
        const y = baseY - bins[b] * ampH;
        ctx.lineTo(x, y);
      }
      ctx.lineTo(ox + w, baseY);
      ctx.closePath();
      // 填色：愈新（前）愈不透明；以峰值顏色微調，營造景深
      const fade = 0.30 + 0.55 * (1 - dz);
      ctx.fillStyle = rgbStr([rgb[0] * 0.5, rgb[1] * 0.5, rgb[2] * 0.5].map(Math.round), fade);
      ctx.fill();
      // 脊線（top line）
      ctx.beginPath();
      let started = false;
      for (let b = 0; b < N_BINS; b += STEP) {
        const x = ox + (b / (N_BINS - 1)) * w;
        const y = baseY - bins[b] * ampH;
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = rgbStr(rgb, 0.35 + 0.6 * (1 - dz));
      ctx.lineWidth = r === 0 ? 1.6 : 1;
      ctx.stroke();
    }

    // 前緣頻道刻度
    ctx.font = "9px -apple-system";
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(255,255,255,.4)";
    const dz0 = 0, ox0 = originX(dz0), w0 = rowW(dz0), by0 = baseYof(dz0);
    const [fMin, fMax] = BAND_FREQ[_band];
    for (const ch of TICKS[_band]) {
      const f = chFreq(ch, _band);
      const fx = (f - fMin) / (fMax - fMin);
      if (fx < 0 || fx > 1) continue;
      ctx.fillText(ch, ox0 + fx * w0, by0 + 16);
    }

    // 視角提示
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(255,255,255,.3)";
    ctx.fillText("拖曳旋轉視角 · 新列在前", padL, padT - 2);
  }

  let _raf3d = 0;
  function schedule3D() {
    if (_raf3d) return;
    _raf3d = requestAnimationFrame(() => { _raf3d = 0; if (_wfMode === "3d") draw3DWaterfall(); });
  }
  let _rafRender = 0;
  function scheduleRender() {
    if (_rafRender) return;
    _rafRender = requestAnimationFrame(() => { _rafRender = 0; if (active()) render(); });
  }

  function bind3DInteraction() {
    if (_wf3dBound) return;
    const canvas = document.getElementById("spa-waterfall-canvas");
    if (!canvas) return;
    _wf3dBound = true;
    let dragging = false, lx = 0, ly = 0;
    canvas.addEventListener("pointerdown", e => {
      if (_wfMode !== "3d") return;
      dragging = true; lx = e.clientX; ly = e.clientY;
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", e => {
      if (!dragging || _wfMode !== "3d") return;
      const dx = e.clientX - lx, dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      _d3.skewX  = Math.max(-1, Math.min(1, _d3.skewX + dx * 0.004));
      _d3.depthY = Math.max(0.25, Math.min(0.92, _d3.depthY + dy * 0.003));
      schedule3D();          // rAF 合併：一幀最多重繪一次
    });
    const stop = e => { dragging = false; try { canvas.releasePointerCapture(e.pointerId); } catch (_) {} };
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointercancel", stop);
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
        `ch${escHtml(busiest[0])} <span style="font-size:11px;color:var(--text-2)">${escHtml(busiest[1])} AP</span>`;
    } else {
      document.getElementById("spa-strongest").textContent = "—";
      document.getElementById("spa-busiest").textContent   = "—";
    }
  }

  // ── 錄製 / 儲存 / 載入 / 回放 ────────────────────────────────────────────────
  function updateRecLabel() {
    const btn = document.getElementById("spa-record");
    btn.lastChild.textContent = _recording ? ` 錄製中 ${_recBuf.length}` : "錄製";
  }

  function toggleRecord() {
    if (_mode === "playback") return;
    _recording = !_recording;
    if (_recording) _recBuf = [];
    document.getElementById("spa-record").classList.toggle("active", _recording);
    updateRecLabel();
  }

  function saveRecording() {
    const buf = _recBuf;
    if (!buf.length) { alert("尚無錄製資料，請先按「錄製」收集 sweep。"); return; }
    const data = {
      format: "wifi-survey-pro-spectrum",
      version: 1,
      created: new Date().toISOString(),
      band: _band,
      sweeps: buf,
    };
    const blob = new Blob([JSON.stringify(data)], { type: "application/json" });
    const url  = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const ts = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    a.href = url;
    a.download = `wifi-spectrum-${ts}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function loadRecordingFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = JSON.parse(reader.result);
        const sweeps = data.sweeps || data;
        if (!Array.isArray(sweeps) || !sweeps.length) throw new Error("空檔案");
        _recBuf = sweeps;
        if (data.band && BAND_FREQ[data.band]) {
          _band = data.band;
          syncBandButtons();
        }
        enterPlayback();
      } catch (e) {
        alert("無法載入錄製檔：" + e.message);
      }
    };
    reader.readAsText(file);
  }

  function enterPlayback() {
    if (_recording) toggleRecord();
    stopPlay();
    _mode = "playback";
    _playIdx = 0;
    resetSpectrumState();        // 新的 _recBuf：增量狀態作廢
    // 載入時一次算好每個 sweep 的 bins（錄製來的已在錄製時快取），之後 seek 只做累加
    for (let i = 0; i < _recBuf.length; i++) sweepBins(i);
    document.getElementById("spa-playback").hidden = false;
    document.getElementById("spa-record").disabled = true;
    const seek = document.getElementById("spa-seek");
    seek.max = String(Math.max(0, _recBuf.length - 1));
    seek.value = "0";
    loadFrame(0);
  }

  function exitPlayback() {
    stopPlay();
    _mode = "live";
    document.getElementById("spa-playback").hidden = true;
    document.getElementById("spa-record").disabled = false;
    resetSpectrumState();
    _sweeps = 0;
    render();
  }

  // 第 i 個 sweep 在目前頻段的 bins（快取；換頻段時自動重算）
  function sweepBins(i) {
    const sw = _recBuf[i];
    const c = _binCache.get(sw);
    if (c && c.band === _band) return c.bins;
    const bins = computeBins(sw.nets || []);
    _binCache.set(sw, { band: _band, bins });
    return bins;
  }

  function accumulate(bins) {
    for (let b = 0; b < N_BINS; b++) {
      if (bins[b] > _maxHold[b]) _maxHold[b] = bins[b];
      _avgBins[b] = _avgInit ? _avgBins[b] * (1 - AVG_A) + bins[b] * AVG_A : bins[b];
    }
    _avgInit = true;
  }

  // 重建到第 idx 個 sweep：rows = [idx-MAX_ROWS+1 .. idx]，maxHold/avg 從 0..idx 累積
  // （呈現「到目前為止」的保持與平均）。連續播放 idx = 上一格 + 1 時只增量處理一個
  // sweep；跳轉（seek）則用快取的 bins 重新累積，不再每格重算 computeBins。
  function loadFrame(idx) {
    idx = Math.max(0, Math.min(_recBuf.length - 1, idx));
    _playIdx = idx;
    if (_pbAccIdx >= 0 && idx === _pbAccIdx + 1 && _pbAccBand === _band) {
      const bins = sweepBins(idx);
      accumulate(bins);
      _rows.unshift({ bins });
      if (_rows.length > MAX_ROWS) _rows.length = MAX_ROWS;
    } else if (!(_pbAccIdx === idx && _pbAccBand === _band)) {
      resetSpectrumState();
      const start = Math.max(0, idx - MAX_ROWS + 1);
      for (let i = 0; i <= idx; i++) {
        const bins = sweepBins(i);
        accumulate(bins);
        if (i >= start) _rows.unshift({ bins });
      }
    }
    _pbAccIdx = idx; _pbAccBand = _band;
    _lastNets = _recBuf[idx].nets || [];
    _sweeps = idx + 1;
    document.getElementById("spa-seek").value = String(idx);
    document.getElementById("spa-play-label").textContent =
      `${idx + 1} / ${_recBuf.length}`;
    render();
  }

  function playStep() {
    // 分頁 / 視窗不可見：暫停計時器，回到可見時再續播（見 resumePlayIfVisible）
    if (!active() || document.hidden) {
      if (_playTimer) { clearInterval(_playTimer); _playTimer = null; }
      _playSuspended = true;
      return;
    }
    if (_playIdx >= _recBuf.length - 1) { stopPlay(); return; }
    loadFrame(_playIdx + 1);
  }

  function startPlayTimer() {
    const iv = (parseFloat(document.getElementById("spa-interval").value) || 2) * 1000;
    _playTimer = setInterval(playStep, Math.max(200, iv));
  }

  function startPlay() {
    if (_playIdx >= _recBuf.length - 1) loadFrame(0);
    _playing = true;
    _playSuspended = false;
    document.getElementById("spa-play").textContent = "❚❚ 暫停";
    startPlayTimer();
  }

  function resumePlayIfVisible() {
    if (_playing && _playSuspended && !_playTimer && active() && !document.hidden) {
      _playSuspended = false;
      startPlayTimer();
    }
  }

  function stopPlay() {
    _playing = false;
    _playSuspended = false;
    if (_playTimer) { clearInterval(_playTimer); _playTimer = null; }
    const btn = document.getElementById("spa-play");
    if (btn) btn.textContent = "▶ 播放";
  }

  // ── Controls ───────────────────────────────────────────────────────────────
  function syncBandButtons() {
    document.querySelectorAll("#spa-bands .iperf-mode-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.band === _band));
  }

  document.querySelectorAll("#spa-bands .iperf-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#spa-bands .iperf-mode-btn")
        .forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _band = btn.dataset.band;
      resetSpectrumState();          // 不同頻段的頻率軸不同，重置累積狀態
      if (_mode === "playback") loadFrame(_playIdx);
      else render();
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

  // 疊加分析 toggles
  function bindToggle(id, set) {
    document.getElementById(id).addEventListener("click", e => {
      const on = e.currentTarget.classList.toggle("active");
      set(on);
      render();
    });
  }
  bindToggle("spa-maxhold", v => _showMaxHold = v);
  bindToggle("spa-avg",     v => _showAvg = v);
  bindToggle("spa-peak",    v => _showPeak = v);

  document.getElementById("spa-reset-hold").addEventListener("click", () => {
    _maxHold = new Float32Array(N_BINS);
    _avgBins = new Float32Array(N_BINS);
    _avgInit = false;
    _pbAccIdx = -1;      // 回放下一格整段重建（與原本每格重建的行為一致）
    render();
  });

  // 瀑布 2D / 3D 切換
  document.querySelectorAll("#spa-wf-mode .iperf-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#spa-wf-mode .iperf-mode-btn")
        .forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _wfMode = btn.dataset.wf;
      document.getElementById("spa-wf-title").textContent = _wfMode === "3d"
        ? "瀑布圖 · 3D 立體（拖曳旋轉，新列在前）"
        : "瀑布圖（新 → 舊，顏色 = 訊號強度）";
      render();
    });
  });

  // 錄製 / 儲存 / 載入
  document.getElementById("spa-record").addEventListener("click", toggleRecord);
  document.getElementById("spa-save").addEventListener("click", saveRecording);
  document.getElementById("spa-load").addEventListener("click", () =>
    document.getElementById("spa-file").click());
  document.getElementById("spa-file").addEventListener("change", e => {
    if (e.target.files[0]) loadRecordingFile(e.target.files[0]);
    e.target.value = "";
  });

  // 回放控制
  document.getElementById("spa-play").addEventListener("click", () =>
    _playing ? stopPlay() : startPlay());
  // 拖曳進度條：rAF 合併，一幀最多載入一次
  let _seekRaf = 0, _seekIdx = 0;
  document.getElementById("spa-seek").addEventListener("input", e => {
    stopPlay();
    _seekIdx = parseInt(e.target.value, 10) || 0;
    if (_seekRaf) return;
    _seekRaf = requestAnimationFrame(() => { _seekRaf = 0; loadFrame(_seekIdx); });
  });
  document.getElementById("spa-exit-play").addEventListener("click", exitPlayback);

  // 優化建議：依目前頻段的頻道壅塞 / 最佳頻道產生建議方案
  document.getElementById("spa-reco-btn").addEventListener("click", () => {
    const payload = { band: _band };
    if (_lastNets.length) payload.networks = _lastNets;
    Reco.fetchAndRender("spa-reco", "spectrum", payload, { scroll: true });
  });

  // 切到本分頁時立即重繪（canvas 在隱藏時量不到尺寸）
  document.querySelector('.nav-item[data-tab="spectrumtab"]')
    ?.addEventListener("click", () => setTimeout(() => { render(); resumePlayIfVisible(); }, 50));
  document.addEventListener("visibilitychange", resumePlayIfVisible);
  window.addEventListener("resize", scheduleRender);   // rAF 合併 resize 重繪

  tick();   // start loop（非 active 時只是空轉計時器）

  return { render };
})();
