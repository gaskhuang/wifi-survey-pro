/* ═══════════════════════════════════════════════════════════════════════════
   traffic.js — 流量異常監測分頁
   即時 top talkers + 廣播/pps 統計；結束後以優化建議面板列出異常。
   ═══════════════════════════════════════════════════════════════════════════ */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;" }[c]));
  let _es = null, _ifaceLoaded = false;

  async function loadIface() {
    if (_ifaceLoaded) return;
    try {
      const d = await (await fetch("/api/traffic/interfaces")).json();
      $("tf-iface").innerHTML = (d.interfaces || [])
        .map(i => `<option value="${esc(i.name)}">${esc(i.name)}${i.is_default ? " (預設)" : ""}</option>`).join("");
      if (!d.can_capture) {
        $("tf-perm").textContent = "⚠ 目前無封包擷取權限：請安裝 Wireshark 的 ChmodBPF，或以系統管理員權限啟動。";
        $("tf-perm").classList.remove("hidden");
      }
      _ifaceLoaded = true;
    } catch (_) {}
  }

  function renderTalkers(talkers) {
    if (!talkers || !talkers.length) return;
    $("tf-tbody").innerHTML = talkers.map((t, i) => {
      const ba = (t.bcast || t.arp)
        ? `${t.bcast ? "廣播" + t.bcast : ""}${t.bcast && t.arp ? " / " : ""}${t.arp ? "ARP" + t.arp : ""}` : "—";
      const hot = t.share >= 50 ? ' style="color:#f87171;font-weight:700"' : "";
      return `<tr>
        <td>${i + 1}</td><td>${esc(t.mac)}</td><td>${esc(t.vendor || "未知")}</td>
        <td>${esc(t.ip || "—")}</td><td>${t.pkts}</td><td>${t.pps}</td>
        <td${hot}>${t.share}%</td><td class="svc">${ba}</td></tr>`;
    }).join("");
  }

  function applyStats(s) {
    $("tf-pkts").textContent    = s.total_pkts;
    $("tf-pps").textContent     = s.pps;
    $("tf-bcast").textContent   = s.bcast_pct + "%";
    $("tf-sources").textContent = s.sources;
    if (s.duration) {
      $("tf-progress-bar").style.width =
        Math.min(100, Math.round(s.elapsed / s.duration * 100)) + "%";
    }
    renderTalkers(s.talkers);
  }

  function start() {
    const iface = $("tf-iface").value;
    const duration = $("tf-duration").value;
    if (_es) { _es.close(); _es = null; }
    $("tf-tbody").innerHTML = "";
    $("tf-reco").hidden = true;
    $("tf-progress-wrap").classList.remove("hidden");
    $("tf-progress-bar").style.width = "0%";
    $("btn-tf-start").disabled = true;
    $("btn-tf-stop").disabled = false;
    setStatus(`監聽 ${iface}…（${duration} 秒，可隨時停止）`);

    _es = new EventSource(`/api/traffic/stream?iface=${encodeURIComponent(iface)}&duration=${duration}`);
    _es.addEventListener("stats", e => applyStats(JSON.parse(e.data)));
    _es.addEventListener("done", e => {
      const r = JSON.parse(e.data);
      if (r.snapshot) applyStats(r.snapshot);
      Reco.render("tf-reco", r, { title: "流量異常分析" });
      setStatus("擷取完成");
      finish();
    });
    _es.addEventListener("error", e => {
      let msg = "擷取失敗"; try { msg = JSON.parse(e.data).error || msg; } catch (_) {}
      setStatus(msg, true);
      if (msg.includes("權限") || msg.includes("BPF")) {
        $("tf-perm").textContent = "⚠ " + msg; $("tf-perm").classList.remove("hidden");
      }
      finish();
    });
    _es.onerror = () => { if ($("btn-tf-start").disabled) { setStatus("連線中斷", true); finish(); } };
  }

  function stop() { if (_es) { _es.close(); _es = null; } setStatus("已停止"); finish(); }

  function finish() {
    if (_es) { _es.close(); _es = null; }
    $("btn-tf-start").disabled = false;
    $("btn-tf-stop").disabled = true;
    $("tf-progress-wrap").classList.add("hidden");
  }

  function setStatus(msg, err = false) {
    const el = $("tf-status"); el.textContent = msg;
    el.classList.remove("hidden"); el.style.color = err ? "#f87171" : "";
  }

  $("btn-tf-start").addEventListener("click", start);
  $("btn-tf-stop").addEventListener("click", stop);
  document.querySelector('.nav-item[data-tab="traffic"]')
    ?.addEventListener("click", () => setTimeout(loadIface, 60));
})();
