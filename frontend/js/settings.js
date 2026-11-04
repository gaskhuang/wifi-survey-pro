/* ═══════════════════════════════════════════════════════════════════════════
   settings.js — 設定分頁（AI 分析引擎 / 字型大小 / 白標 / 隱私）
   AI 引擎：一個下拉選單選「供應商・模型」，填該供應商的 API 金鑰，按儲存就生效。
   金鑰採「留空 = 不變更」；畫面只顯示遮蔽後的金鑰，不回傳明文。
   ═══════════════════════════════════════════════════════════════════════════ */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => window.escHtml(s);
  const CUSTOM = "__custom";
  // 申請金鑰的網址只顯示成文字（App 視窗裡點連結會把整個介面導走）
  const KEY_SITE = {
    openai:   "platform.openai.com/api-keys",
    gemini:   "aistudio.google.com/apikey",
    deepseek: "platform.deepseek.com/api_keys",
    claude:   "platform.claude.com/settings/keys",
  };
  let _loaded = false, _cat = null, _s = null;
  const _extra = {};          // 從供應商載入的模型清單：provider → [id]

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify(body) });
    return r.json();
  }

  function setStatus(msg, err = false, el = $("set-status")) {
    el.textContent = msg;
    el.style.color = err ? "#f87171" : "#4ade80";
    if (!err) setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 5000);
  }

  // ── 下拉選單 ─────────────────────────────────────────────────────────────
  function buildSelect(sel) {
    const ai = _s.ai, P = _cat.providers;
    let html = `<option value="offline">離線規則引擎（免金鑰・不連網）</option>`;
    if (_cat.codex_available || ai.provider === "codex")
      html += `<option value="codex">Codex CLI（使用本機登入，免金鑰）</option>`;
    for (const p of _cat.keyed) {
      const ids = [];
      const notes = {};
      (_cat.models[p] || []).forEach(m => { ids.push(m.id); notes[m.id] = m.note; });
      (_extra[p] || []).forEach(id => { if (!ids.includes(id)) ids.push(id); });
      const cur = ai.models[p];
      if (cur && !ids.includes(cur)) ids.unshift(cur);          // 之前自訂的模型也列出來
      const keyTag = ai.keys_set[p] ? "" : "・需金鑰";
      html += `<optgroup label="${esc(P[p])}${keyTag}">` +
        ids.map(id => `<option value="${esc(p)}:${esc(id)}">${esc(P[p])}・${esc(id)}${notes[id] ? `（${esc(notes[id])}）` : ""}</option>`).join("") +
        `<option value="${esc(p)}:${CUSTOM}">${esc(P[p])}・其他模型（自行輸入）…</option></optgroup>`;
    }
    $("ai-model").innerHTML = html;
    $("ai-model").value = sel;
    if ($("ai-model").value !== sel) $("ai-model").value = "offline";
    onSelect();
  }

  const selected = () => {
    const v = $("ai-model").value || "offline";
    const i = v.indexOf(":");
    return i < 0 ? { provider: v, model: "" } : { provider: v.slice(0, i), model: v.slice(i + 1) };
  };

  function onSelect() {
    const { provider, model } = selected();
    const keyed = _cat.keyed.includes(provider);
    $("ai-key-row").classList.toggle("hidden", !keyed);
    $("ai-custom-row").classList.toggle("hidden", model !== CUSTOM);
    if (model === CUSTOM) $("ai-custom-model").focus();
    let note = "";
    if (keyed) {
      const name = _cat.providers[provider];
      $("ai-key-label").textContent = "API 金鑰";
      const set = _s.ai.keys_set[provider];
      $("ai-key").placeholder = set ? `已設定（${_s.ai.keys_masked[provider]}）— 留空不變更` : `貼上 ${name} 的 API 金鑰`;
      note = `${set ? "金鑰已儲存在這台 Mac。" : "還沒有金鑰？"}申請網址：${KEY_SITE[provider]}`;
    } else if (provider === "codex") {
      note = _cat.codex_available ? "使用這台 Mac 上已登入的 Codex CLI 帳號。" : "這台 Mac 找不到 Codex CLI，分析時會改用離線規則引擎。";
    } else {
      note = "依內建規則判讀，不需要網路與金鑰。";
    }
    $("ai-key-note").textContent = note;
  }

  function collectAI() {
    const { provider, model } = selected();
    const ai = { provider };
    if (_cat.keyed.includes(provider)) {
      const m = model === CUSTOM ? $("ai-custom-model").value.trim() : model;
      if (!m) throw new Error("請輸入模型名稱");
      if (!/^[A-Za-z0-9][A-Za-z0-9._:\/-]{0,79}$/.test(m)) throw new Error("模型名稱只能包含英數字與 . _ : / -");
      ai.models = { [provider]: m };
      const k = $("ai-key").value.trim();
      if (k) ai.keys = { [provider]: k };
      else if (!_s.ai.keys_set[provider]) throw new Error(`請填入 ${_cat.providers[provider]} 的 API 金鑰`);
    }
    return ai;
  }

  function currentValue() {
    const p = _s.ai.provider;
    return _cat.keyed.includes(p) ? `${p}:${_s.ai.models[p]}` : p;
  }

  // ── 載入 / 儲存 ───────────────────────────────────────────────────────────
  async function load() {
    try {
      const [sd, cd] = await Promise.all([
        fetch("/api/settings").then(r => r.json()),
        fetch("/api/ai/catalog").then(r => r.json()),
      ]);
      _s = sd.settings; _cat = cd;
      buildSelect(currentValue());
      $("ui-font").value = String(_s.ui?.font_scale ?? 1);
      $("brand-company").value = _s.branding.company || "";
      $("brand-contact").value = _s.branding.contact || "";
      $("priv-plaintext").checked = !!_s.privacy.plaintext_logs;
      _loaded = true;
    } catch (e) {
      setStatus("讀取設定失敗：" + e.message, true);
    }
  }

  async function saveAI() {
    let ai;
    try { ai = collectAI(); } catch (e) { setStatus(e.message, true); return false; }
    const d = await post("/api/settings", { ai });
    if (!d.ok) { setStatus(d.error || "儲存失敗", true); return false; }
    _s = d.settings;
    $("ai-key").value = "";
    $("ai-custom-model").value = "";
    buildSelect(currentValue());
    window.dispatchEvent(new CustomEvent("settings:saved"));   // AI 診斷 / 現場診斷更新引擎提示
    return true;
  }

  async function save() {
    if (await saveAI()) {
      const p = _s.ai.provider;
      setStatus(`已儲存 ✓ 之後的 AI 分析會使用${_cat.keyed.includes(p) ? `「${_cat.providers[p]}・${_s.ai.models[p]}」` : `「${_cat.providers[p]}」`}`);
    }
  }

  async function test() {
    setStatus("測試中…（新一代模型可能要十幾秒）", false);
    if (!(await saveAI())) return;               // 先存，確保測試用的是剛輸入的金鑰與模型
    const provider = _s.ai.provider;
    const d = await post("/api/ai/test", { provider });
    if (d.ok) setStatus(`✓ ${d.message || "連線成功"}`);
    else setStatus(`✗ ${d.error}`, true);
  }

  async function loadModels() {
    const { provider } = selected();
    if (!_cat.keyed.includes(provider)) return;
    const btn = $("btn-ai-models");
    setLoading(btn, true);
    try {
      const d = await post("/api/ai/models", { provider, key: $("ai-key").value.trim() });
      if (!d.ok) { setStatus(d.error, true); return; }
      if (!d.models.length) { setStatus("供應商沒有回傳可用的模型", true); return; }
      _extra[provider] = d.models;
      const keep = $("ai-model").value;
      buildSelect(keep.endsWith(CUSTOM) ? `${provider}:${d.models[0]}` : keep);
      setStatus(`已載入 ${d.models.length} 個 ${_cat.providers[provider]} 模型，請從選單挑選`);
    } catch (e) {
      setStatus("載入失敗：" + e.message, true);
    } finally {
      setLoading(btn, false);
    }
  }

  async function saveFont() {
    const v = parseFloat($("ui-font").value) || 1;
    applyFontScale(v);                                 // 立即套用，不重新載入頁面
    const d = await post("/api/settings", { ui: { font_scale: v } });
    setStatus(d.ok ? `字型大小已設為 ${Math.round(v * 100)}% ✓` : (d.error || "儲存失敗"), !d.ok, $("ui-font-status"));
  }

  async function saveBrand() {
    const d = await post("/api/settings", {
      branding: { company: $("brand-company").value.trim(), contact: $("brand-contact").value.trim() } });
    setStatus(d.ok ? "白標已儲存 ✓" : "儲存失敗", !d.ok);
  }

  $("ai-model").addEventListener("change", onSelect);
  $("btn-set-save").addEventListener("click", save);
  $("btn-set-test").addEventListener("click", test);
  $("btn-ai-models").addEventListener("click", loadModels);
  $("ui-font").addEventListener("change", saveFont);
  $("btn-brand-save").addEventListener("click", saveBrand);
  $("priv-plaintext").addEventListener("change", e =>
    post("/api/settings", { privacy: { plaintext_logs: e.target.checked } })
      .then(() => setStatus("已更新隱私設定 ✓")));

  // 延遲載入：第一次開啟設定分頁才抓（失敗時下次開啟再重試）
  window.addEventListener("tab:activated", e => {
    if (e.detail?.tab === "settings" && !_loaded) load();
  });
  if ($("tab-settings")?.classList.contains("active")) load();
})();
