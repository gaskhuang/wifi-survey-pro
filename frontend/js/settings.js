/* ═══════════════════════════════════════════════════════════════════════════
   settings.js — 設定分頁（AI 供應商 / 白標 / 隱私）
   金鑰採「留空 = 不變更」；顯示遮蔽狀態，不回傳明文金鑰。
   ═══════════════════════════════════════════════════════════════════════════ */
(() => {
  const $ = id => document.getElementById(id);
  let _loaded = false;

  async function load() {
    try {
      const d = await (await fetch("/api/settings")).json();
      const s = d.settings; const ai = s.ai;
      // provider
      const r = document.querySelector(`input[name="ai-provider"][value="${ai.provider}"]`);
      if (r) r.checked = true;
      // models
      ["openai", "gemini", "deepseek", "claude"].forEach(p => {
        if ($("model-" + p)) $("model-" + p).value = ai.models[p] || "";
        // 已設定的金鑰：placeholder 提示，不填明文
        if ($("key-" + p)) $("key-" + p).placeholder =
          ai.keys_set[p] ? `已設定（${ai.keys_masked[p]}）— 留空不變更` : "尚未設定";
      });
      $("cloud-endpoint").value = ai.cloud.endpoint || "";
      $("cloud-token").placeholder = ai.cloud.token_set ? "已設定 — 留空不變更" : "訂閱授權 token";
      // branding / privacy
      $("brand-company").value = s.branding.company || "";
      $("brand-contact").value = s.branding.contact || "";
      $("priv-plaintext").checked = !!s.privacy.plaintext_logs;
      _loaded = true;
    } catch (e) {
      setStatus("讀取設定失敗：" + e.message, true);
    }
  }

  function setStatus(msg, err = false) {
    const el = $("set-status");
    el.textContent = msg;
    el.style.color = err ? "#f87171" : "#4ade80";
    if (!err) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 4000);
  }

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body) });
    return r.json();
  }

  function collectAI() {
    const provider = (document.querySelector('input[name="ai-provider"]:checked') || {}).value || "offline";
    const keys = {}, models = {};
    ["openai", "gemini", "deepseek", "claude"].forEach(p => {
      const k = $("key-" + p).value.trim();
      if (k) keys[p] = k;                        // 只送有輸入的（空 = 不變更）
      const m = $("model-" + p).value.trim();
      if (m) models[p] = m;
    });
    const cloud = { endpoint: $("cloud-endpoint").value.trim() };
    const ct = $("cloud-token").value.trim();
    if (ct) cloud.token = ct;
    return { provider, keys, models, cloud };
  }

  async function save() {
    const d = await post("/api/settings", { ai: collectAI() });
    if (d.ok) {
      setStatus("已儲存 ✓");
      // 清空密碼欄並重載遮蔽狀態
      ["openai", "gemini", "deepseek", "claude"].forEach(p => $("key-" + p).value = "");
      $("cloud-token").value = "";
      load();
    } else setStatus("儲存失敗", true);
  }

  async function test() {
    const provider = (document.querySelector('input[name="ai-provider"]:checked') || {}).value || "offline";
    setStatus("測試中…");
    // 先存目前輸入，確保測試用的是最新金鑰
    await post("/api/settings", { ai: collectAI() });
    ["openai", "gemini", "deepseek", "claude"].forEach(p => $("key-" + p).value = "");
    $("cloud-token").value = "";
    const d = await post("/api/ai/test", { provider });
    if (d.ok) setStatus(`✓ ${provider}：${d.message || "連線成功"}${d.sample ? "（" + d.sample + "）" : ""}`);
    else setStatus(`✗ ${provider}：${d.error}`, true);
    load();
  }

  async function saveBrand() {
    const d = await post("/api/settings", {
      branding: { company: $("brand-company").value.trim(), contact: $("brand-contact").value.trim() } });
    setStatus(d.ok ? "白標已儲存 ✓" : "儲存失敗", !d.ok);
  }

  $("btn-set-save").addEventListener("click", save);
  $("btn-set-test").addEventListener("click", test);
  $("btn-brand-save").addEventListener("click", saveBrand);
  $("priv-plaintext").addEventListener("change", e =>
    post("/api/settings", { privacy: { plaintext_logs: e.target.checked } })
      .then(() => setStatus("已更新隱私設定 ✓")));

  document.querySelector('.nav-item[data-tab="settings"]')
    ?.addEventListener("click", () => { if (!_loaded) load(); });
  load();
})();
