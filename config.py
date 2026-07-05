"""
config.py — 使用者設定儲存（NetInspect Pro）

存於使用者可寫入目錄（打包後 .app 內唯讀，設定不能放 bundle 內）：
  macOS  : ~/Library/Application Support/NetInspectPro/config.json
  其他   : ~/.config/netinspectpro/config.json

主要保存 AI 引擎設定：
  · provider：offline（離線規則）/ openai / gemini / deepseek / claude / cloud（雲端代理）
  · keys：各家 BYOK 金鑰（存使用者本機；不寫入任何診斷 log）
  · cloud：進階付費雲端分析主機端點與授權 token（雲端樁，之後接）

安全：診斷資料與 AI 請求/回應「不落地明文 log」；金鑰僅存本設定檔，
      回傳給前端時一律遮蔽（masked），避免外洩。
"""
import os
import sys
import json
import threading

_LOCK = threading.Lock()

_DEFAULT = {
    "ai": {
        "provider": "offline",                # offline|openai|gemini|deepseek|claude|cloud
        "keys": {"openai": "", "gemini": "", "deepseek": "", "claude": ""},
        "models": {
            "openai": "gpt-4o-mini",
            "gemini": "gemini-1.5-flash",
            "deepseek": "deepseek-chat",
            "claude": "claude-sonnet-5",
        },
        "cloud": {"endpoint": "", "token": ""},  # 進階付費雲端分析（樁）
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
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """讀取完整設定（與預設合併，確保欄位齊全）。"""
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return _deep_merge(_DEFAULT, data)
    except Exception:
        return json.loads(json.dumps(_DEFAULT))   # deep copy


def save(patch: dict) -> dict:
    """以 patch 深度合併後寫回，回傳新設定。"""
    with _LOCK:
        cur = load()
        merged = _deep_merge(cur, patch or {})
        try:
            with open(_config_path(), "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return merged


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
            "cloud": {"endpoint": ai["cloud"]["endpoint"],
                      "token_set": bool(ai["cloud"]["token"])},
        },
        "privacy": c["privacy"],
        "branding": {"company": c["branding"]["company"],
                     "contact": c["branding"]["contact"],
                     "logo_set": bool(c["branding"]["logo_data"])},
    }


if __name__ == "__main__":
    print("config path:", _config_path())
    print(json.dumps(public_view(), ensure_ascii=False, indent=2))
