/* ═══════════════════════════════════════════════════════════════════════════
   reco.js — 共用「優化建議方案」面板
   每個功能跑完測試後呼叫 Reco.fetchAndRender()，向 /api/recommend 取得
   聚焦該功能的優化建議，並以與診斷分頁相同的卡片樣式呈現。
   ═══════════════════════════════════════════════════════════════════════════ */
const Reco = (() => {

  const SEV_ORDER  = ["critical", "warning", "info", "good"];
  const SEV_LABEL  = { critical:"🔴 嚴重問題", warning:"🟡 警告", info:"🔵 優化建議", good:"🟢 通過項目" };
  const SEV_BADGE  = { critical:"sev-critical", warning:"sev-warning", info:"sev-info", good:"sev-good" };
  const SEV_TXT    = { critical:"嚴重", warning:"警告", info:"建議", good:"通過" };
  const COUNT_META = [
    { sev:"critical", label:"嚴重", color:"#ef4444" },
    { sev:"warning",  label:"警告", color:"#eab308" },
    { sev:"info",     label:"建議", color:"#3b82f6" },
    { sev:"good",     label:"良好", color:"#22c55e" },
  ];

  const esc = s => window.escHtml(s);   // 共用 app.js 版本（含引號，可用於屬性）

  function panelHTML(a, title) {
    const { score, grade, issues, counts } = a;
    const counded = COUNT_META.map(({ sev, label, color }) =>
      `<span class="reco-count"><b style="color:${color}">${esc(counts[sev])}</b> ${label}</span>`).join("");

    let body = "";
    for (const sev of SEV_ORDER) {
      const group = issues.filter(i => i.severity === sev);
      if (!group.length) continue;
      body += `<div class="diag-section-title">${SEV_LABEL[sev]}（${group.length}）</div>`;
      body += group.map(issue => `
        <div class="diag-issue ${esc(issue.severity)}">
          <div class="diag-issue-icon">${esc(issue.icon || "•")}</div>
          <div class="diag-issue-body">
            <div class="diag-issue-title">${esc(issue.title)}</div>
            ${issue.problem ? `<div class="diag-issue-problem">${esc(issue.problem)}</div>` : ""}
            <div class="diag-issue-rec">${esc(issue.recommendation)}</div>
          </div>
          <div>
            <span class="diag-sev-badge ${SEV_BADGE[issue.severity]}">${SEV_TXT[issue.severity]}</span>
            <div style="font-size: calc(12px * var(--fs, 1));color:var(--text-3);margin-top:3px;text-align:center">${esc(issue.category)}</div>
          </div>
        </div>`).join("");
    }

    return `
      <div class="reco-head">
        <div class="reco-title">💡 ${esc(title || "優化建議方案")}</div>
        <div class="reco-score">
          <span class="reco-score-num" style="color:${esc(grade.color)}">${esc(score)}</span>
          <span class="reco-grade" style="color:${esc(grade.color)}">${esc(grade.emoji)} ${esc(grade.label)}</span>
        </div>
        <div class="reco-counts">${counded}</div>
      </div>
      <div class="reco-issues">${body}</div>`;
  }

  const _last = {};   // containerId → { assessment, title }（匯出報告用）

  function render(containerId, assessment, opts = {}) {
    const el = document.getElementById(containerId);
    if (!el) return;
    _last[containerId] = { assessment, title: opts.title || "優化建議方案" };
    el.innerHTML = panelHTML(assessment, opts.title);
    el.hidden = false;
    if (opts.scroll) el.scrollIntoView({ behavior:"smooth", block:"nearest" });
  }

  async function fetchAndRender(containerId, context, payload = {}, opts = {}) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.hidden = false;
    delete _last[containerId];
    el.innerHTML = `<div class="reco-loading"><span class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,.2);border-top-color:#3b82f6"></span> 產生優化建議中…</div>`;
    try {
      const r = await fetch("/api/recommend", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ context, ...payload }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || "建議產生失敗");
      render(containerId, d.assessment, opts);
    } catch (e) {
      el.innerHTML = `<div class="reco-loading" style="color:#ef4444">⚠ 優化建議產生失敗：${esc(e.message)}</div>`;
    }
  }

  const last = id => _last[id] || null;
  return { render, fetchAndRender, last };
})();
