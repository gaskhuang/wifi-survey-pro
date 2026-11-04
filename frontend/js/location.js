/* 定位授權提示：macOS 沒有定位授權時，CoreWLAN 回傳的 SSID / BSSID 全部被遮蔽
 * （表格會顯示 Hidden Network、<需定位授權>）。這裡依授權狀態顯示對應的按鈕。 */
(function () {
  "use strict";
  const banner = document.getElementById("loc-banner");
  if (!banner) return;

  const PATH = "系統設定 › 隱私權與安全性 › 定位服務";
  let polling = null;
  let lastStatus = null;

  async function getStatus() {
    try { return await (await fetch("/api/location/status")).json(); }
    catch (_) { return null; }
  }

  function hide() { banner.classList.add("hidden"); banner.innerHTML = ""; }

  function show(html, btnLabel, onClick) {
    banner.innerHTML = `<div class="loc-text">${html}</div>` +
      (btnLabel ? `<button class="btn btn-primary" id="loc-btn">${btnLabel}</button>` : "");
    banner.classList.remove("hidden");
    const b = document.getElementById("loc-btn");
    if (b) b.addEventListener("click", onClick);
  }

  async function post(url) {
    try { return await (await fetch(url, { method: "POST" })).json(); }
    catch (_) { return { ok: false }; }
  }

  const openSettings = () => post("/api/location/open-settings");

  // 授權狀態改變（使用者按了允許、或從系統設定回來）→ 自動重新掃描
  function watch(ms = 60000) {
    clearInterval(polling);
    const until = Date.now() + ms;
    polling = setInterval(async () => {
      const st = await getStatus();
      if (st && st.authorized) {
        clearInterval(polling);
        hide();
        if (typeof doScan === "function") doScan();
      } else if (Date.now() > until) {
        clearInterval(polling);
        if (st) render(st);
      }
    }, 1500);
  }

  function render(st) {
    lastStatus = st;
    if (!st || !st.available || st.authorized) { hide(); return; }
    if (st.services_enabled === false) {
      show(`<b>Mac 的定位服務總開關是關閉的</b>，系統因此遮蔽所有 Wi-Fi 的網路名稱與 BSSID。<br>
            <span class="loc-path">請到 ${PATH}，打開最上方的「定位服務」，再勾選 NetInspect Pro。</span>`,
           "開啟系統設定", () => { openSettings(); watch(180000); });
      return;
    }
    if (!st.app_bundle) {
      show(`<b>開發模式：</b>定位授權由啟動本程式的終端機 / App 決定。<br>
            <span class="loc-path">請到 ${PATH}，替該終端機開啟定位，或改用打包好的 NetInspect Pro.app。</span>`,
           "開啟系統設定", () => { openSettings(); watch(180000); });
      return;
    }
    if (st.status === "not_determined") {
      show(`<b>需要定位授權才能看到網路名稱和 BSSID。</b>macOS 規定掃描 Wi-Fi 的 App 必須取得定位授權，
            NetInspect Pro 不會記錄或上傳你的位置。`,
           "授權定位服務", async () => {
             await post("/api/location/request");
             show(`請在跳出的系統視窗按「允許」。<span class="loc-path">沒看到視窗的話，請到 ${PATH} 手動勾選 NetInspect Pro。</span>`,
                  "開啟系統設定", () => { openSettings(); watch(180000); });
             watch();
           });
      return;
    }
    // denied / restricted：系統不會再跳視窗，只能到系統設定改
    show(`<b>定位授權已被關閉</b>，所以網路名稱顯示 Hidden Network、BSSID 顯示 &lt;需定位授權&gt;。<br>
          <span class="loc-path">請到 ${PATH}，找到 NetInspect Pro 並打開開關，回到這裡會自動重新掃描。</span>`,
         "開啟系統設定", () => { openSettings(); watch(180000); });
  }

  async function refresh() { render(await getStatus()); }

  // 從系統設定切回來時重新檢查
  window.addEventListener("focus", () => {
    if (lastStatus && !lastStatus.authorized) refresh().then(() => {
      if (lastStatus && lastStatus.authorized && typeof doScan === "function") doScan();
    });
  });

  window.LocationGate = { refresh };
  refresh();
})();
