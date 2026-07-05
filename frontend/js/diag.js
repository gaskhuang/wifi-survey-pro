/* ═══════════════════════════════════════════════════════════════════════════
   diag.js — AI 診斷分頁
   一鍵情境診斷（OSI/DNS/DHCP/NAT/全項）+ 子網計算機。
   離線規則引擎判讀；設定 AI 供應商後可勾「進階 AI 分析」。
   ═══════════════════════════════════════════════════════════════════════════ */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;" }[c]));
  let _loaded = false;

  const TOPIC_NAME = { all:"一鍵全項", osi:"OSI 由下而上", dns:"DNS vs 斷線",
                       dhcp:"DHCP / IP", nat:"NAT / 路由" };

  async function loadInfo() {
    if (_loaded) return;
    try {
      const d = await (await fetch("/api/diag/info")).json();
      const offline = d.ai_provider === "offline";
      $("dg2-ai-wrap").style.display = offline ? "none" : "inline-flex";
      $("dg2-provider").textContent = offline
        ? "AI 引擎：離線規則（可到「設定」啟用進階 AI）"
        : `AI 引擎：${d.ai_provider}`;
      _loaded = true;
    } catch (_) {}
  }

  function stepsHTML(steps) {
    return steps.map(s =>
      `<div class="dg2-step ${s.ok ? "ok" : "fail"}">
         <span class="dg2-mark">${s.ok ? "✓" : "✗"}</span>
         <span class="dg2-name">${esc(s.name)}</span>
         <span class="dg2-detail">${esc(s.detail)}</span>
       </div>`).join("");
  }

  async function run(topic) {
    $("dg2-status").textContent = `診斷「${TOPIC_NAME[topic] || topic}」中…`;
    $("dg2-reco").hidden = true;
    $("dg2-ai-out").style.display = "none";
    const wantAI = $("dg2-ai").checked;
    try {
      const d = await (await fetch("/api/diag/run", {
        method:"POST", headers:{ "Content-Type":"application/json" },
        body: JSON.stringify({ topic, ai: wantAI }) })).json();
      const r = d.result;
      // 步驟（單一情境有 steps；全項有 groups）
      let steps = r.steps;
      if (!steps && r.groups) steps = r.groups.flatMap(g => g.steps);
      if (steps && steps.length) {
        $("dg2-steps").style.display = "";
        $("dg2-steps-list").innerHTML = stepsHTML(steps);
      } else $("dg2-steps").style.display = "none";
      // 建議面板（共用 Reco）
      Reco.render("dg2-reco", r, { title: `${TOPIC_NAME[topic] || topic} — 診斷結果` });
      // AI 深入分析
      if (r.ai && r.ai.text) {
        $("dg2-ai-out").style.display = "";
        $("dg2-ai-provider").textContent = `（${r.ai.provider}）`;
        $("dg2-ai-text").textContent = r.ai.text;
      } else if (r.ai && r.ai.error) {
        $("dg2-ai-out").style.display = "";
        $("dg2-ai-provider").textContent = "";
        $("dg2-ai-text").textContent = "AI 分析失敗：" + r.ai.error;
      }
      $("dg2-status").textContent = "";
    } catch (e) {
      $("dg2-status").textContent = "診斷失敗：" + e.message;
    }
  }

  async function subnet() {
    const cidr = $("dg2-cidr").value.trim();
    if (!cidr) return;
    try {
      const d = await (await fetch("/api/diag/subnet", {
        method:"POST", headers:{ "Content-Type":"application/json" },
        body: JSON.stringify({ cidr }) })).json();
      if (!d.ok) { $("dg2-subnet-out").innerHTML = `<span class="chip" style="color:#f87171">${esc(d.error)}</span>`; return; }
      const r = d.result;
      const items = [
        ["網段", r.network + "/" + r.prefix], ["子網遮罩", r.netmask],
        ["萬用遮罩", r.wildcard], ["廣播位址", r.broadcast],
        ["可用範圍", `${r.first_host} ~ ${r.last_host}`],
        ["可用主機數", r.num_hosts], ["類型", r.is_private ? "私有" : "公有"],
      ];
      $("dg2-subnet-out").innerHTML = items.map(([k, v]) =>
        `<span class="chip">${esc(k)} <b>${esc(v)}</b></span>`).join("");
    } catch (e) {
      $("dg2-subnet-out").innerHTML = `<span class="chip" style="color:#f87171">${esc(e.message)}</span>`;
    }
  }

  document.querySelectorAll("#tab-diag .dg2-btns button").forEach(b =>
    b.addEventListener("click", () => run(b.dataset.topic)));
  $("btn-dg2-subnet").addEventListener("click", subnet);
  $("dg2-cidr").addEventListener("keydown", e => { if (e.key === "Enter") subnet(); });
  document.querySelector('.nav-item[data-tab="diag"]')
    ?.addEventListener("click", () => setTimeout(loadInfo, 60));
})();
