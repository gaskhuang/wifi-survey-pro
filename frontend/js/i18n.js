/* ═══════════════════════════════════════════════════════════════════════════
   i18n.js — 中 / 英雙語（NetInspect Pro）
   非侵入式：載入時快照介面元素的中文原字，切換英文時以 zh→en 字典替換；
   未收錄的字串維持中文（優雅降級，不會破版）。偏好存 localStorage。
   涵蓋範圍：側邊導覽、分頁標題、區塊標題、主要按鈕與設定/卡片標籤。
   （後端動態產生的內容——如設備類型、診斷建議——目前仍為中文，屬後續階段。）
   ═══════════════════════════════════════════════════════════════════════════ */
const I18N = (() => {

  // ── zh → en 字典 ────────────────────────────────────────────────────────────
  const EN = {
    // 導覽
    "現場診斷": "Field Diagnosis", "掃描": "Scan", "頻道分析": "Channels",
    "頻譜分析儀": "Spectrum", "穩定度分析": "Stability", "覆蓋熱圖": "Heatmap",
    "即時監控": "Live Monitor", "網路效能": "Performance", "有線 LAN": "Wired LAN",
    "流量監測": "Traffic", "AI 診斷": "AI Diagnostics", "報告": "Report", "設定": "Settings",
    // 分頁標題
    "連線穩定度分析": "Connection Stability", "即時訊號監控": "Live Signal Monitor",
    "網路效能測試": "Network Performance Test", "iperf3 吞吐量測試": "iperf3 Throughput Test",
    "有線 LAN 盤查": "Wired LAN Inventory", "流量異常監測": "Traffic Anomaly Monitor",
    "勘測報告": "Survey Report",
    // 常見區塊標題 / 按鈕
    "優化建議": "Optimization Advice", "優化建議方案": "Optimization Plan",
    "開始診斷": "Start Diagnosis", "開始掃描": "Start Scan", "停止": "Stop",
    "匯出 PDF 報告": "Export PDF Report", "匯出 PDF": "Export PDF",
    "產生報告": "Generate Report", "深度指紋": "Deep Fingerprint",
    "設備清單": "Device List", "重新整理": "Refresh", "清除": "Clear",
    "儲存設定": "Save Settings", "儲存白標": "Save Branding", "測試連線": "Test Connection",
    // 設定頁
    "AI 分析引擎": "AI Analysis Engine", "白標（報告抬頭）": "White-label (Report Header)",
    "隱私": "Privacy", "離線規則引擎": "Offline Rule Engine", "進階雲端分析": "Cloud Analysis (Premium)",
    "公司名稱": "Company Name", "聯絡資訊": "Contact Info", "雲端端點": "Cloud Endpoint",
    "雲端授權 Token": "Cloud Auth Token",
    // 卡片標籤（常見）
    "存活設備": "Live Devices", "閘道": "Gateway", "網段": "Subnet", "掃描進度": "Scan Progress",
    "訊號 RSSI": "Signal (RSSI)", "健康分數": "Health Score", "鄰居 AP": "Neighbor APs",
    "下載": "Download", "上傳": "Upload", "掉包": "Packet Loss",
  };

  const TARGETS = ".nav-item, .tab-header h1, .section-title, .card-label, .set-radio span, .set-section .section-title";
  let _snap = [];        // [{el, zh}]
  let _lang = "zh";

  function directTextNodes(el) {
    return [...el.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim());
  }
  function labelOf(el) {
    return directTextNodes(el).map(n => n.nodeValue.trim()).join(" ").trim();
  }
  function setLabel(el, str) {
    const tns = directTextNodes(el);
    if (!tns.length) return;
    tns[0].nodeValue = tns[0].nodeValue.replace(tns[0].nodeValue.trim(), str);
    for (let i = 1; i < tns.length; i++) tns[i].nodeValue = "";
  }

  function snapshot() {
    _snap = [...document.querySelectorAll(TARGETS)].map(el => ({ el, zh: labelOf(el) }))
      .filter(o => o.zh);
  }

  function apply(lang) {
    _lang = lang === "en" ? "en" : "zh";
    for (const { el, zh } of _snap) {
      if (_lang === "en") { if (EN[zh]) setLabel(el, EN[zh]); }
      else setLabel(el, zh);
    }
    document.documentElement.lang = _lang === "en" ? "en" : "zh-TW";
    const btn = document.getElementById("lang-toggle");
    if (btn) btn.textContent = _lang === "en" ? "中文" : "EN";
    try { localStorage.setItem("ni_lang", _lang); } catch (_) {}
  }

  function toggle() { apply(_lang === "en" ? "zh" : "en"); }

  function injectToggle() {
    const nav = document.querySelector("nav");
    if (!nav || document.getElementById("lang-toggle")) return;
    const b = document.createElement("button");
    b.id = "lang-toggle";
    b.className = "lang-toggle";
    b.textContent = "EN";
    b.title = "切換語言 / Switch language";
    b.addEventListener("click", toggle);
    nav.parentNode.insertBefore(b, nav);
  }

  function init() {
    snapshot();
    injectToggle();
    let saved = "zh";
    try { saved = localStorage.getItem("ni_lang") || "zh"; } catch (_) {}
    apply(saved);
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();

  return { apply, toggle, snapshot, t: (zh) => (_lang === "en" ? (EN[zh] || zh) : zh) };
})();
