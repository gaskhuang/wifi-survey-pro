/* ═══════════════════════════════════════════════════════════════════════════
   spectrum.js v3.0 — Wi-Fi Channel Spectrum
   Inspired by tiny-wifi-analyzer (nolze) & WiFi Explorer

   Key features:
   · Bell-curve shapes (Bézier) for each AP — not rectangles
   · SSID labels AT the peak, inside shape, with dark-pill background
   · Consistent color per SSID via djb2 hash (like SHA-1 approach in tiny-wifi-analyzer)
   · Freeze / thaw button — pause live updates per chart
   · SSID filter — type to dim non-matching APs
   · Hover tooltip with BSSID + channel + width + RSSI
   · Correct 5 GHz 40/80/160 MHz channel blocks (exact lookup table from series.py)
   ═══════════════════════════════════════════════════════════════════════════ */

const Spectrum = (() => {

  // ── State ─────────────────────────────────────────────────────────────────
  let _frozen      = false;
  let _filter      = "";           // SSID/BSSID filter text
  let _lastNets    = [];
  let _lastBssid   = "";
  let _hovered     = null;         // network currently under cursor
  let _selectedNet = null;         // network selected via table row click

  // ── Color: use server-assigned net_color (unique per BSSID / channel fingerprint)
  function _networkColor(net) {
    return net.net_color || "#3b82f6";
  }

  // ── Channel / frequency helpers ────────────────────────────────────────────
  function _chFreq(ch, band) {
    if (band === "2.4GHz") return 2407 + ch * 5;
    if (band === "5GHz")   return 5000 + ch * 5;
    if (band === "6GHz")   return 5950 + ch * 5;
    return 0;
  }

  /**
   * Exact 5 GHz channel block lookup (from tiny-wifi-analyzer series.py)
   * Returns [left_ch, right_ch] (channel-unit coordinates, 1 ch = 5 MHz)
   */
  function _channelBlock(ch, widthMHz, band) {
    const h = Math.round(widthMHz / 5) / 2;   // half-span in channel steps

    if (band === "2.4GHz") {
      return [ch - h, ch + h];
    }

    if (band === "6GHz") {
      return [ch - h, ch + h];
    }

    // 5 GHz exact lookup table (mirrors tiny-wifi-analyzer get_channel_block)
    if (band === "5GHz") {
      if (widthMHz === 40) {
        const blocks40 = [
          [36,44],[44,52],[52,60],[60,68],
          [100,108],[108,116],[116,124],[124,132],
          [132,140],[140,148],[149,157],[157,165],[165,173],[173,181],
        ];
        for (const [l, r] of blocks40) {
          if (ch >= l && ch < r) return [l, r];
        }
      }
      if (widthMHz === 80) {
        const blocks80 = [
          [36,52],[52,68],[100,116],[116,132],[132,148],[149,165],[165,181],
        ];
        for (const [l, r] of blocks80) {
          if (ch >= l && ch < r) return [l, r];
        }
      }
      if (widthMHz === 160) {
        const blocks160 = [[36,68],[100,132],[149,181]];
        for (const [l, r] of blocks160) {
          if (ch >= l && ch < r) return [l, r];
        }
      }
      if (widthMHz === 320) return [ch - 32, ch + 32];
      // fallback 20 MHz
      return [ch - 2, ch + 2];
    }

    return [ch - h, ch + h];
  }

  // ── Layout constants ───────────────────────────────────────────────────────
  const BAND_RANGE = {
    "2.4GHz": [2392, 2494],
    "5GHz":   [5160, 5845],
    "6GHz":   [5940, 7130],
  };
  const ML = 38, MR = 38, MT = 18, MB = 38;

  function _layout(has6G) {
    return has6G
      ? [{band:"2.4GHz",start:0.00,w:0.29},{band:"5GHz",start:0.32,w:0.44},{band:"6GHz",start:0.79,w:0.21}]
      : [{band:"2.4GHz",start:0.00,w:0.35},{band:"5GHz",start:0.38,w:0.59}];
  }

  function _freqToX(freq, band, CW, layout) {
    const lo = layout.find(l => l.band === band);
    if (!lo) return ML;
    const [minF, maxF] = BAND_RANGE[band];
    const t = (freq - minF) / (maxF - minF);
    return ML + (lo.start + t * lo.w) * CW;
  }

  function _chToX(ch, band, CW, layout) {
    return _freqToX(_chFreq(ch, band), band, CW, layout);
  }

  // ── Draw ──────────────────────────────────────────────────────────────────
  function draw(networks, currentBssid) {
    const canvas = document.getElementById("spectrum-canvas");
    if (!canvas) return;

    const dpr = window.devicePixelRatio || 1;
    const W   = canvas.offsetWidth;
    const H   = canvas.offsetHeight;
    if (!W || !H) return;

    canvas.width  = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const CW = W - ML - MR;
    const CH = H - MT - MB;

    const has6G  = networks.some(n => n.band === "6GHz");
    const layout = _layout(has6G);

    // ── Background ───────────────────────────────────────────────────────────
    ctx.fillStyle = "#0b0d14";
    ctx.fillRect(0, 0, W, H);

    // ── Grid lines ───────────────────────────────────────────────────────────
    ctx.strokeStyle = "rgba(255,255,255,.055)";
    ctx.lineWidth   = 0.5;
    ctx.setLineDash([3, 6]);
    for (let pct = 10; pct <= 90; pct += 10) {
      const y = MT + CH * (1 - pct / 100);
      ctx.beginPath(); ctx.moveTo(ML, y); ctx.lineTo(ML + CW, y); ctx.stroke();
    }
    ctx.setLineDash([]);

    // ── Y-axis labels ─────────────────────────────────────────────────────────
    ctx.fillStyle = "rgba(255,255,255,.28)";
    ctx.font      = "9px -apple-system";
    for (let pct = 0; pct <= 100; pct += 10) {
      const y = MT + CH * (1 - pct / 100);
      ctx.textAlign = "right";
      ctx.fillText(pct + "%", ML - 5, y + 3);
      ctx.textAlign = "left";
      ctx.fillText(pct + "%", ML + CW + 5, y + 3);
    }

    // ── Band separators + UNII labels ─────────────────────────────────────────
    _drawBandLabels(ctx, W, H, CW, CH, layout, has6G);

    // ── X-axis channel ticks ──────────────────────────────────────────────────
    _drawChannelTicks(ctx, CW, CH, layout);

    // ── AP bell curves — sorted weakest-first so strong signals render on top
    const sorted = [...networks].sort((a, b) => a.signal_pct - b.signal_pct);
    sorted.forEach(net => _drawAP(ctx, net, currentBssid, CW, CH, layout));

    // ── Hovered tooltip handled by mousemove ──────────────────────────────────
  }

  // ── Draw one AP as a bell curve (Bézier) ─────────────────────────────────
  function _drawAP(ctx, net, currentBssid, CW, CH, layout) {
    const { band, channel, signal_pct, ssid, bssid } = net;
    const width      = parseInt(net.width) || 20;
    const color      = _networkColor(net);
    const isConn     = bssid === currentBssid;
    const isHovered  = _hovered  && _hovered._idx  === net._idx;
    const isSelected = _selectedNet && _selectedNet._idx === net._idx;

    // Filter: dim non-matching networks
    const matchFilter = !_filter ||
      (ssid || "").toLowerCase().includes(_filter) ||
      (bssid|| "").toLowerCase().includes(_filter);
    const alpha = matchFilter ? 1 : 0.12;

    // Channel block in channel units
    const [cL, cR] = _channelBlock(channel, width, band);

    // Convert to pixel X
    const xPeak = _chToX(channel, band, CW, layout);
    const xL    = _chToX(cL, band, CW, layout);
    const xR    = _chToX(cR, band, CW, layout);

    // Y: signal_pct maps to height
    const h    = (signal_pct / 100) * CH;
    const yBot = MT + CH;
    const yTop = yBot - h;

    // Clamp to visible area
    const clampL = Math.max(xL, ML);
    const clampR = Math.min(xR, ML + CW);
    if (clampR - clampL <= 1) return;

    ctx.save();
    ctx.globalAlpha = alpha;

    // ── Bell curve path (Bézier from left baseline → peak → right baseline) ──
    ctx.beginPath();
    ctx.moveTo(clampL, yBot);

    // Left shoulder → peak
    ctx.bezierCurveTo(
      clampL + (xPeak - clampL) * 0.4, yBot,   // cp1
      clampL + (xPeak - clampL) * 0.6, yTop,   // cp2
      xPeak, yTop                                // peak
    );

    // Peak → right shoulder
    ctx.bezierCurveTo(
      xPeak + (clampR - xPeak) * 0.4, yTop,    // cp1
      xPeak + (clampR - xPeak) * 0.6, yBot,    // cp2
      clampR, yBot                               // end
    );

    ctx.closePath();

    // Fill with gradient — selected/connected brighter, hovered slightly brighter
    const grad = ctx.createLinearGradient(0, yTop, 0, yBot);
    const topAlpha = (isSelected || isConn) ? "cc" : isHovered ? "99" : "55";
    grad.addColorStop(0, color + topAlpha);
    grad.addColorStop(1, color + "18");
    ctx.fillStyle = grad;
    ctx.fill();

    // Stroke outline
    ctx.strokeStyle = color + ((isSelected || isConn || isHovered) ? "ff" : "88");
    ctx.lineWidth   = (isSelected || isConn) ? 2.5 : isHovered ? 2 : 1;
    ctx.stroke();

    ctx.restore();

    // ── SSID label at peak ────────────────────────────────────────────────────
    if (matchFilter) {
      _drawLabel(ctx, net, xPeak, yTop, clampL, clampR, color, isConn || isSelected, isHovered, CH);
    }
  }

  // ── SSID label with background pill ──────────────────────────────────────
  function _drawLabel(ctx, net, xPeak, yTop, xL, xR, color, isConn, isHovered, CH) {
    const ssid = net.ssid;
    const showLabel = ssid && ssid !== "<Hidden>";
    const bandText  = `ch${net.channel}`;
    // Format: "ch11 SSID" (truncate SSID if too long)
    const maxChars  = Math.max(4, Math.floor((xR - xL) / 6.5));
    const ssidTrunc = showLabel
      ? (ssid.length > maxChars ? ssid.slice(0, maxChars - 1) + "…" : ssid)
      : null;
    const labelText = ssidTrunc ? `${bandText} ${ssidTrunc}` : bandText;

    // Font size: bigger for connected/hovered
    const fontSize = isConn || isHovered ? 11 : 9;
    ctx.font = `${isConn ? "700" : "500"} ${fontSize}px -apple-system`;
    const textW = ctx.measureText(labelText).width;
    const textH = fontSize + 2;

    // Position: place label at peak, centered
    const lx = Math.max(xL + 2, Math.min(xPeak, xL + (xR - xL) / 2));
    const ly = yTop - 5;

    // Background pill
    const padX = 5, padY = 2;
    const rx   = lx - textW / 2 - padX;
    const ry   = ly - textH + padY;
    const rw   = textW + padX * 2;
    const rh   = textH + padY;

    ctx.save();
    ctx.globalAlpha = isConn || isHovered ? 0.95 : 0.82;

    // Pill background
    const bgAlpha = isConn || isHovered ? "dd" : "bb";
    ctx.fillStyle = "#0b0d14" + bgAlpha;
    _roundRect(ctx, rx, ry, rw, rh, 3);
    ctx.fill();

    // Pill border (network color)
    ctx.strokeStyle = color + (isConn ? "cc" : "77");
    ctx.lineWidth   = isConn ? 1.5 : 0.8;
    _roundRect(ctx, rx, ry, rw, rh, 3);
    ctx.stroke();

    // Text
    ctx.fillStyle = isConn ? "#ffffff" : (color + "ee");
    ctx.textAlign = "center";
    ctx.fillText(labelText, lx, ly);
    ctx.restore();
  }

  function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x+w, y, x+w, y+r, r);
    ctx.lineTo(x+w, y+h-r);
    ctx.arcTo(x+w, y+h, x+w-r, y+h, r);
    ctx.lineTo(x+r, y+h);
    ctx.arcTo(x, y+h, x, y+h-r, r);
    ctx.lineTo(x, y+r);
    ctx.arcTo(x, y, x+r, y, r);
    ctx.closePath();
  }

  // ── Band / UNII labels ─────────────────────────────────────────────────────
  const UNII_5G = [
    {label:"UNII-1",  cStart:36,  cEnd:48 },
    {label:"UNII-2A", cStart:52,  cEnd:64 },
    {label:"UNII-2C", cStart:100, cEnd:144},
    {label:"UNII-3",  cStart:149, cEnd:165},
    {label:"UNII-4",  cStart:169, cEnd:181},
  ];

  function _drawBandLabels(ctx, W, H, CW, CH, layout, has6G) {
    layout.forEach(lo => {
      const x0 = ML + lo.start * CW;
      const x1 = ML + (lo.start + lo.w) * CW;

      if (lo.start > 0) {
        ctx.strokeStyle = "rgba(255,255,255,.07)";
        ctx.lineWidth   = 1;
        ctx.beginPath();
        ctx.moveTo(x0 - 8, MT);
        ctx.lineTo(x0 - 8, H - 5);
        ctx.stroke();
      }

      ctx.fillStyle = "rgba(255,255,255,.2)";
      ctx.font      = "bold 9px -apple-system";
      ctx.textAlign = "center";
      ctx.fillText(lo.band === "2.4GHz" ? "ISM / 2.4 GHz" : lo.band, (x0+x1)/2, H - 3);
    });

    // UNII sub-band labels for 5 GHz
    const lo5 = layout.find(l => l.band === "5GHz");
    if (lo5) {
      UNII_5G.forEach(({label, cStart, cEnd}) => {
        const xA = _chToX(cStart, "5GHz", CW, layout) - 8;
        const xB = _chToX(cEnd,   "5GHz", CW, layout) + 8;
        if (xA > ML + CW || xB < ML) return;
        const cx = Math.max(xA, ML) + (Math.min(xB, ML+CW) - Math.max(xA, ML)) / 2;

        ctx.fillStyle   = "rgba(80,100,180,.18)";
        ctx.strokeStyle = "rgba(80,100,180,.3)";
        ctx.lineWidth   = 0.5;
        ctx.fillRect(xA, MT + CH + 2, xB - xA, 12);
        ctx.strokeRect(xA, MT + CH + 2, xB - xA, 12);

        ctx.fillStyle = "rgba(160,190,255,.65)";
        ctx.font      = "8px -apple-system";
        ctx.textAlign = "center";
        ctx.fillText(label, cx, MT + CH + 11);
      });
    }
  }

  // ── Channel number ticks ───────────────────────────────────────────────────
  function _drawChannelTicks(ctx, CW, CH, layout) {
    ctx.fillStyle   = "rgba(255,255,255,.33)";
    ctx.strokeStyle = "rgba(255,255,255,.13)";
    ctx.lineWidth   = 0.5;
    ctx.font        = "8px -apple-system";
    ctx.textAlign   = "center";

    const NON_OV = new Set([1, 6, 11]);

    // 2.4 GHz ch 1-13
    for (let ch = 1; ch <= 13; ch++) {
      const x = _chToX(ch, "2.4GHz", CW, layout);
      if (x < ML || x > ML+CW) continue;
      if (NON_OV.has(ch)) {
        ctx.fillStyle = "rgba(96,165,250,.8)";
        ctx.font      = "bold 8px -apple-system";
      } else {
        ctx.fillStyle = "rgba(255,255,255,.33)";
        ctx.font      = "8px -apple-system";
      }
      ctx.fillText(ch, x, MT + CH + MB * 0.42);
      ctx.beginPath(); ctx.moveTo(x, MT+CH); ctx.lineTo(x, MT+CH+3); ctx.stroke();
    }

    // 5 GHz major ticks
    const ticks5 = [36,40,44,48,52,56,60,64,100,108,116,124,132,140,144,149,153,157,161,165];
    ctx.fillStyle = "rgba(255,255,255,.33)";
    ctx.font      = "8px -apple-system";
    ticks5.forEach(ch => {
      const x = _chToX(ch, "5GHz", CW, layout);
      if (x < ML || x > ML+CW) return;
      ctx.fillText(ch, x, MT + CH + MB * 0.42);
      ctx.beginPath(); ctx.moveTo(x, MT+CH); ctx.lineTo(x, MT+CH+3); ctx.stroke();
    });

    // 6 GHz ticks if present
    const lo6 = layout.find(l => l.band === "6GHz");
    if (lo6) {
      [1,5,9,13,21,37,53,69,85].forEach(ch => {
        const x = _chToX(ch, "6GHz", CW, layout);
        if (x < ML || x > ML+CW) return;
        ctx.fillText(ch, x, MT + CH + MB * 0.42);
      });
    }
  }

  // ── Hover detection ────────────────────────────────────────────────────────
  function _setupInteraction(networks, currentBssid) {
    const canvas = document.getElementById("spectrum-canvas");
    if (!canvas) return;

    // Tooltip element
    let tooltip = document.getElementById("spectrum-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.id = "spectrum-tooltip";
      tooltip.style.cssText = [
        "position:absolute","pointer-events:none","z-index:20",
        "background:rgba(10,12,20,.92)","border:1px solid rgba(255,255,255,.12)",
        "border-radius:6px","padding:7px 11px","font-size:11px",
        "color:#e2e8f0","line-height:1.7","display:none",
        "white-space:nowrap","box-shadow:0 4px 16px rgba(0,0,0,.5)",
      ].join(";");
      canvas.parentElement.appendChild(tooltip);
    }

    canvas.onmousemove = e => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const W  = canvas.offsetWidth;
      const H  = canvas.offsetHeight;
      const CW = W - ML - MR;
      const CH = H - MT - MB;
      // Always use _lastNets so hover reflects the actual drawn snapshot (frozen or live)
      const has6G  = _lastNets.some(n => n.band === "6GHz");
      const layout = _layout(has6G);

      const hit = _hitTest(mx, my, _lastNets, CW, CH, layout);
      if (hit !== _hovered) {
        _hovered = hit;
        draw(_lastNets, _lastBssid);   // redraw to highlight
      }

      if (hit) {
        const bssidText = hit.bssid?.startsWith("<") ? "需定位授權" : hit.bssid;
        tooltip.innerHTML = [
          `<strong style="color:${_networkColor(hit)}">${hit.ssid || "(Hidden)"}</strong>`,
          `BSSID: <span style="font-family:monospace;font-size:10px">${bssidText}</span>`,
          `訊號: <strong>${hit.rssi} dBm</strong> &nbsp; SNR: ${hit.snr} dB &nbsp; ${hit.signal_pct}%`,
          `頻道: <strong>ch${hit.channel}</strong> &nbsp; ${hit.band} &nbsp; ${hit.width} MHz`,
          `標準: ${hit.standard}`,
          hit.vendor ? `廠商: ${hit.vendor}` : "",
        ].filter(Boolean).join("<br>");

        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        tooltip.style.display = "block";
        tooltip.style.left    = Math.min(cx + 14, W - 220) + "px";
        tooltip.style.top     = Math.max(0, cy - 20) + "px";

        // Highlight matching rows in table
        document.querySelectorAll("#net-tbody tr.net-row").forEach(tr => {
          tr.classList.toggle("spectrum-hover",
            tr.dataset.bssid === hit.bssid || tr.dataset.ssid === hit.ssid);
        });
      } else {
        tooltip.style.display = "none";
        document.querySelectorAll("#net-tbody tr.net-row.spectrum-hover").forEach(tr =>
          tr.classList.remove("spectrum-hover"));
      }
    };

    canvas.onmouseleave = () => {
      _hovered = null;
      tooltip.style.display = "none";
      document.querySelectorAll("#net-tbody tr.net-row.spectrum-hover").forEach(tr =>
        tr.classList.remove("spectrum-hover"));
      draw(_lastNets, _lastBssid);
    };

    canvas.onclick = e => {
      if (!_hovered) return;
      // Clicking an AP selects it in spectrum and scrolls the table row into view
      _selectedNet = _hovered;
      draw(_lastNets, _lastBssid);
      const tr = document.querySelector(`#net-tbody tr.net-row[data-idx="${_hovered._idx}"]`);
      if (tr) {
        document.querySelectorAll("#net-tbody tr.net-row.selected").forEach(r => r.classList.remove("selected"));
        tr.classList.add("selected");
        tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    };
  }

  function _hitTest(mx, my, networks, CW, CH, layout) {
    const has6G = networks.some(n => n.band === "6GHz");
    if (!has6G) layout = _layout(false);

    // Test in reverse order (front-to-back — strongest signal on top)
    const sorted = [...networks].sort((a,b) => b.signal_pct - a.signal_pct);

    for (const net of sorted) {
      const { band, channel, signal_pct } = net;
      const width = parseInt(net.width) || 20;
      const [cL, cR] = _channelBlock(channel, width, band);
      const xPeak = _chToX(channel, band, CW, layout);
      const xL    = Math.max(_chToX(cL, band, CW, layout), ML);
      const xR    = Math.min(_chToX(cR, band, CW, layout), ML + CW);
      const h     = (signal_pct / 100) * CH;
      const yBot  = MT + CH;
      const yTop  = yBot - h;

      // Simple bounding-box hit test (slightly wider for usability)
      if (mx >= xL - 2 && mx <= xR + 2 && my >= yTop - 16 && my <= yBot) {
        return net;
      }
    }
    return null;
  }

  // ── Frozen badge overlay ──────────────────────────────────────────────────
  function _updateFrozenBadge(frozen, wrap) {
    let badge = document.getElementById("spectrum-frozen-badge");
    if (frozen) {
      if (!badge) {
        badge = document.createElement("div");
        badge.id = "spectrum-frozen-badge";
        badge.textContent = "● PAUSED";
        badge.style.cssText = [
          "position:absolute","bottom:28px","left:8px","z-index:10",
          "font-size:9px","font-weight:700","letter-spacing:.08em",
          "color:#22c55e","pointer-events:none",
          "text-shadow:0 0 8px rgba(34,197,94,.5)",
        ].join(";");
        wrap.appendChild(badge);
      }
    } else if (badge) {
      badge.remove();
    }
  }

  // ── Controls: freeze button + filter input ────────────────────────────────
  function _setupControls() {
    const wrap   = document.querySelector(".spectrum-wrap");
    if (!wrap || document.getElementById("spectrum-freeze-btn")) return;

    // Freeze button
    const freezeBtn = document.createElement("button");
    freezeBtn.id    = "spectrum-freeze-btn";
    freezeBtn.title = "暫停/恢復即時更新";
    freezeBtn.innerHTML = `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> 暫停`;
    freezeBtn.style.cssText = [
      "position:absolute","top:5px","right:56px","z-index:10",
      "padding:3px 9px","background:rgba(30,30,50,.8)",
      "border:1px solid rgba(255,255,255,.15)","border-radius:4px",
      "font-size:10px","color:rgba(255,255,255,.6)","cursor:pointer",
      "display:flex","align-items:center","gap:4px",
    ].join(";");
    freezeBtn.addEventListener("click", () => {
      _frozen = !_frozen;
      freezeBtn.innerHTML = _frozen
        ? `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> 恢復`
        : `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> 暫停`;
      freezeBtn.style.color = _frozen ? "#22c55e" : "rgba(255,255,255,.6)";
      freezeBtn.style.borderColor = _frozen ? "rgba(34,197,94,.5)" : "rgba(255,255,255,.15)";

      // Unfreeze: immediately redraw + rebind interactions with latest data
      if (!_frozen && _lastNets.length) {
        draw(_lastNets, _lastBssid);
        _setupInteraction(_lastNets, _lastBssid);
      }

      _updateFrozenBadge(_frozen, wrap);
    });
    wrap.appendChild(freezeBtn);

    // Filter input
    const filterInput = document.createElement("input");
    filterInput.id    = "spectrum-filter";
    filterInput.type  = "text";
    filterInput.placeholder = "過濾 SSID/MAC";
    filterInput.style.cssText = [
      "position:absolute","top:5px","right:140px","z-index:10",
      "padding:3px 8px","background:rgba(30,30,50,.8)",
      "border:1px solid rgba(255,255,255,.15)","border-radius:4px",
      "font-size:10px","color:#e2e8f0","width:110px",
      "outline:none",
    ].join(";");
    filterInput.addEventListener("input", e => {
      _filter = e.target.value.toLowerCase().trim();
      filterInput.style.borderColor = _filter ? "rgba(59,130,246,.6)" : "rgba(255,255,255,.15)";
      if (_lastNets.length) draw(_lastNets, _lastBssid);
    });
    filterInput.addEventListener("focus", () => { filterInput.style.borderColor = "rgba(59,130,246,.6)"; });
    filterInput.addEventListener("blur",  () => {
      if (!_filter) filterInput.style.borderColor = "rgba(255,255,255,.15)";
    });
    wrap.appendChild(filterInput);
  }

  // ── Public: update ────────────────────────────────────────────────────────
  function update(networks, currentBssid) {
    const canvas = document.getElementById("spectrum-canvas");
    if (!canvas) return;

    // Size canvas to container
    const wrap = canvas.parentElement;
    canvas.style.width  = wrap.clientWidth  + "px";
    canvas.style.height = wrap.clientHeight + "px";

    // Only overwrite snapshot with non-empty data — a temporary empty scan
    // (adapter hiccup, channel switch) should not wipe the frozen frame.
    if (networks.length > 0) {
      _lastNets  = networks;
      _lastBssid = currentBssid;
    }

    _setupControls();

    if (_frozen) return;   // don't redraw or rebind interactions when frozen

    draw(networks, currentBssid);
    _setupInteraction(networks, currentBssid);
  }

  // ── Public: setHighlight (called from table row click) ────────────────────
  function setHighlight(net) {
    _selectedNet = net;
    if (_lastNets.length) draw(_lastNets, _lastBssid);
  }

  return { update, setHighlight };
})();
