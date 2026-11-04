"""
macOS 定位服務授權。

macOS 14 起，沒有定位授權的程式從 CoreWLAN 拿到的 SSID / BSSID 一律是空值（系統遮蔽）。
這裡負責：查詢授權狀態、跳出系統授權視窗、帶使用者到系統設定。

注意：
- 授權綁在「App 身分」上。打包版由原生啟動程式內嵌 Python，NSBundle.mainBundle 是
  NetInspect Pro.app，Info.plist 有 NSLocationWhenInUseUsageDescription，才能跳視窗。
  開發模式直接跑 python 時沒有這個身分，授權由啟動它的終端機 / App 決定。
- 只為了解除 SSID 遮蔽而請求授權；收到第一筆位置或授權結果就立刻停止定位，座標不保存。
"""
import subprocess
import threading

_STATUS_NAMES = {
    0: "not_determined",   # 還沒問過
    1: "restricted",       # 家長控制 / MDM 限制
    2: "denied",           # 使用者拒絕
    3: "authorized",       # macOS 的「允許」
    4: "authorized",       # authorizedWhenInUse
}

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices"

_lock = threading.Lock()
_mgr = None
_delegate = None

try:
    import CoreLocation                       # pyobjc-framework-CoreLocation
    from Foundation import NSObject, NSBundle
    _AVAILABLE = True
except Exception:                             # 非 macOS 或缺套件
    _AVAILABLE = False


def _stop(mgr):
    try:
        mgr.stopUpdatingLocation()
    except Exception:
        pass


def _on_auth_change():
    try:
        import scanner
        scanner.invalidate_cache()            # 授權後下一次掃描要重新取，不能回傳遮蔽過的快取
    except Exception:
        pass


if _AVAILABLE:
    class NIPLocationDelegate(NSObject):
        # 只實作參數全是物件的委派方法（macOS 11+），避免 PyObjC 簽章猜錯
        def locationManagerDidChangeAuthorization_(self, mgr):
            if mgr.authorizationStatus() != 0:
                _stop(mgr)
            _on_auth_change()

        def locationManager_didUpdateLocations_(self, mgr, locations):
            _stop(mgr)                        # 不需要位置本身

        def locationManager_didFailWithError_(self, mgr, error):
            _stop(mgr)


def _current_code() -> int:
    if _mgr is not None:
        return int(_mgr.authorizationStatus())
    try:
        return int(CoreLocation.CLLocationManager.alloc().init().authorizationStatus())
    except Exception:
        return int(CoreLocation.CLLocationManager.authorizationStatus())


def status() -> dict:
    """回傳授權狀態與 App 身分資訊（前端據此決定顯示「授權」或「開啟系統設定」）。"""
    if not _AVAILABLE:
        return {"ok": True, "available": False, "status": "unavailable", "authorized": False}
    try:
        code = _current_code()
        enabled = bool(CoreLocation.CLLocationManager.locationServicesEnabled())
        bundle = NSBundle.mainBundle()
        has_desc = bundle.objectForInfoDictionaryKey_("NSLocationWhenInUseUsageDescription") is not None
        name = _STATUS_NAMES.get(code, "unknown")
        return {
            "ok": True,
            "available": True,
            "status": name,
            "code": code,
            "authorized": name == "authorized",
            "services_enabled": enabled,          # 系統層級的「定位服務」總開關
            "app_bundle": has_desc,               # False＝開發模式，無法自行跳授權視窗
        }
    except Exception as e:
        return {"ok": False, "available": True, "status": "error", "authorized": False, "error": str(e)}


def request() -> dict:
    """請系統跳出定位授權視窗（只有「還沒問過」時會跳；拒絕過就只能去系統設定改）。
    CLLocationManager 必須在有 run loop 的主執行緒建立，委派回呼才收得到。"""
    if not _AVAILABLE:
        return {"ok": False, "error": "此系統不支援定位服務"}

    def _do():
        global _mgr, _delegate
        with _lock:
            if _mgr is None:
                _mgr = CoreLocation.CLLocationManager.alloc().init()
                _delegate = NIPLocationDelegate.alloc().init()
                _mgr.setDelegate_(_delegate)
            mgr = _mgr
        if mgr.authorizationStatus() != 0:
            return
        if mgr.respondsToSelector_("requestWhenInUseAuthorization"):
            mgr.requestWhenInUseAuthorization()
        mgr.startUpdatingLocation()            # 部分 macOS 版本只有開始定位時才會跳視窗
        try:
            from PyObjCTools import AppHelper
            AppHelper.callLater(60.0, _stop, mgr)   # 使用者一直不回應也不會持續定位
        except Exception:
            pass

    try:
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_do)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def request_if_needed():
    """App 啟動時呼叫：第一次開啟就跳授權視窗，App 也會因此出現在系統設定的定位清單。"""
    st = status()
    if st.get("available") and st.get("app_bundle") and st.get("status") == "not_determined":
        request()


def open_settings() -> dict:
    try:
        subprocess.Popen(["/usr/bin/open", SETTINGS_URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
