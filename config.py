"""
config.py — 使用者設定儲存（NetInspect Pro）

存於使用者可寫入目錄（打包後 .app 內唯讀，設定不能放 bundle 內）：
  macOS  : ~/Library/Application Support/NetInspectPro/config.json
  其他   : ~/.config/netinspectpro/config.json

主要保存：
  · ai.provider：offline（離線規則）/ codex / openai / gemini / deepseek / claude
  · ai.models：各供應商選用的模型；ai.keys：各家 API 金鑰（存使用者本機；不寫入任何診斷 log）
  · ui.font_scale：介面字型大小倍率

安全：診斷資料與 AI 請求/回應「不落地明文 log」；金鑰僅存本設定檔，
      回傳給前端時一律遮蔽（masked），避免外洩。
"""
import os
import sys
import copy
import json
import time
import threading

_LOCK = threading.RLock()     # save() 內會呼叫 load()，需可重入
_CACHE = {"key": None, "data": None}   # 依 (mtime_ns, size) 快取解析結果

_DEFAULT = {
    "ai": {
        "provider": "offline",                # offline|codex|openai|gemini|deepseek|claude
        "keys": {"openai": "", "gemini": "", "deepseek": "", "claude": ""},
        "models": {                           # 與 ai_providers.DEFAULT_MODEL 一致
            "openai": "gpt-6-astra",
            "gemini": "gemini-3.8-flash",
            "deepseek": "deepseek-flash",
            "claude": "claude-opus-5-5",
        },
    },
    "ui": {
        "font_scale": 1.0,                    # 介面字型大小倍率（0.8 ～ 1.5）
    },
    "privacy": {
        "plaintext_logs": False,              # 地端不留明文診斷 log
    },
    "branding": {                              # 白標（報告抬頭）
        "company": "", "contact": "", "logo_data": "",
    },
}


def _config_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/NetInspectPro")
    else:
        base = os.path.join(os.environ.get("APPDATA")
                            or os.path.expanduser("~/.config"), "netinspectpro")
    os.makedirs(base, exist_ok=True)
    return base


def _config_path() -> str:
    return os.path.join(_config_dir(), "config.json")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict):
            # 既有為 dict 子樹：只接受 dict 深合併，拒絕純量/None 覆蓋（避免破壞結構）
            if isinstance(v, dict):
                out[k] = _deep_merge(out[k], v)
            # 否則忽略此次覆蓋，保留原 dict
        else:
            out[k] = v
    return out


_PROVIDERS = ("offline", "codex", "openai", "gemini", "deepseek", "claude")
FONT_SCALES = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5)


def _migrate(c: dict) -> dict:
    """舊版設定升級：拿掉已移除的「進階雲端分析」、已停用的舊預設模型換成新預設、字級夾在範圍內。"""
    ai = c["ai"]
    ai.pop("cloud", None)
    if ai.get("provider") not in _PROVIDERS:
        ai["provider"] = "offline"
    try:
        import ai_providers
        for p, old in ai_providers.LEGACY_MODELS.items():
            if not ai["models"].get(p) or ai["models"][p] in old:
                ai["models"][p] = ai_providers.DEFAULT_MODEL[p]
    except Exception:
        pass
    try:
        fs = float(c["ui"].get("font_scale", 1.0))
    except (TypeError, ValueError):
        fs = 1.0
    c["ui"]["font_scale"] = min(FONT_SCALES, key=lambda x: abs(x - fs))
    return c


def _backup_corrupt(path: str):
    """設定檔損毀時另存備份，避免之後的 save() 把唯一的資料蓋掉。"""
    try:
        os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}.bak")
    except OSError:
        pass


def load() -> dict:
    """讀取完整設定（與預設合併，確保欄位齊全）。依檔案 mtime/大小快取，避免每次 get() 都重讀解析。"""
    path = _config_path()
    with _LOCK:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return copy.deepcopy(_DEFAULT)
        except OSError:
            return copy.deepcopy(_DEFAULT)
        key = (st.st_mtime_ns, st.st_size)
        if _CACHE["key"] == key and _CACHE["data"] is not None:
            return copy.deepcopy(_CACHE["data"])
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("設定檔內容不是物件")
        except (ValueError, OSError):
            # 損毀（JSON 解析失敗）：先備份原檔再以預設值繼續，資料不會被默默洗掉
            _backup_corrupt(path)
            _CACHE["key"], _CACHE["data"] = None, None
            return copy.deepcopy(_DEFAULT)
        merged = _migrate(_deep_merge(_DEFAULT, data))
        _CACHE["key"], _CACHE["data"] = key, merged
        return copy.deepcopy(merged)


def save(patch: dict) -> dict:
    """以 patch 深度合併後寫回，回傳新設定。
    原子寫入：先寫暫存檔並 fsync，再 os.replace，讀取端永遠只看到完整的舊檔或新檔
    （直接 open(path,"w") 會先清空檔案，斷電/崩潰就可能留下半個檔、金鑰全失）。
    寫檔失敗會拋出例外（由呼叫端處理）。"""
    with _LOCK:
        merged = _migrate(_deep_merge(load(), patch or {}))
        path = _config_path()
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 含 API 金鑰，僅本人可讀
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _CACHE["key"], _CACHE["data"] = None, None      # 下次 load() 依新 mtime 重建快取
        return copy.deepcopy(merged)


def get(path: str, default=None):
    """點路徑取值，例 get('ai.provider')。"""
    node = load()
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _mask(k: str) -> str:
    if not k:
        return ""
    return (k[:3] + "••••" + k[-4:]) if len(k) > 8 else "••••"


def public_view() -> dict:
    """回給前端的安全版：金鑰遮蔽，只顯示是否已設定。"""
    c = load()
    ai = c["ai"]
    return {
        "ai": {
            "provider": ai["provider"],
            "models": ai["models"],
            "keys_set": {k: bool(v) for k, v in ai["keys"].items()},
            "keys_masked": {k: _mask(v) for k, v in ai["keys"].items()},
        },
        "ui": c["ui"],
        "privacy": c["privacy"],
        "branding": {"company": c["branding"]["company"],
                     "contact": c["branding"]["contact"],
                     "logo_set": bool(c["branding"]["logo_data"])},
    }


if __name__ == "__main__":
    print("config path:", _config_path())
    print(json.dumps(public_view(), ensure_ascii=False, indent=2))
