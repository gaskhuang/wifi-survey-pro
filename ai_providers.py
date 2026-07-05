"""
ai_providers.py — 多家 AI 供應商路由（NetInspect Pro）

三種模式（依 config.ai.provider）：
  · offline           ：離線規則引擎（由 recommendations / recommender 負責，本模組回 None）
  · openai / deepseek ：OpenAI 相容 Chat Completions（BYOK 自備金鑰）
  · gemini            ：Google Generative Language API（BYOK）
  · claude            ：Anthropic Messages API（BYOK）
  · cloud             ：進階付費——地端把資料上傳「業主雲端分析主機」，回傳結果地端只顯示
                        （opaque：地端不知分析方式；不落地明文 log）

僅用標準庫 urllib，無額外相依。金鑰只從 config 讀取，永不寫入 log。
"""
import json
import urllib.request
import urllib.error

import config

_TIMEOUT = 40


def _post(url: str, headers: dict, body: dict, timeout: int = _TIMEOUT) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _openai_compatible(base: str, key: str, model: str, system: str, user: str) -> str:
    d = _post(f"{base}/chat/completions",
              {"Authorization": f"Bearer {key}"},
              {"model": model, "temperature": 0.2,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]})
    return d["choices"][0]["message"]["content"]


def _gemini(key: str, model: str, system: str, user: str) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    d = _post(url, {}, {"contents": [{"parts": [{"text": system + "\n\n" + user}]}]})
    return d["candidates"][0]["content"]["parts"][0]["text"]


def _claude(key: str, model: str, system: str, user: str) -> str:
    d = _post("https://api.anthropic.com/v1/messages",
              {"x-api-key": key, "anthropic-version": "2023-06-01"},
              {"model": model, "max_tokens": 1500, "system": system,
               "messages": [{"role": "user", "content": user}]})
    return d["content"][0]["text"]


def call_provider(provider: str, system: str, user: str) -> str:
    """呼叫指定供應商，回傳文字。offline 由上層處理，不應呼叫此函式。"""
    ai = config.load()["ai"]
    keys, models = ai["keys"], ai["models"]
    if provider == "openai":
        return _openai_compatible("https://api.openai.com/v1", keys["openai"],
                                  models["openai"], system, user)
    if provider == "deepseek":
        return _openai_compatible("https://api.deepseek.com/v1", keys["deepseek"],
                                  models["deepseek"], system, user)
    if provider == "gemini":
        return _gemini(keys["gemini"], models["gemini"], system, user)
    if provider == "claude":
        return _claude(keys["claude"], models["claude"], system, user)
    if provider == "cloud":
        cloud = ai["cloud"]
        if not cloud.get("endpoint"):
            raise RuntimeError("未設定雲端分析主機端點")
        # 進階付費：地端只上傳資料、顯示結果（不解析分析方式、不落地 log）
        return json.dumps(_post(cloud["endpoint"],
                                {"Authorization": f"Bearer {cloud.get('token','')}"},
                                {"payload": user}), ensure_ascii=False)
    raise RuntimeError(f"供應商 {provider} 不支援線上呼叫")


def test(provider: str) -> dict:
    """測試供應商連線（設定頁用）。金鑰缺失時給明確提示，不外洩金鑰。"""
    ai = config.load()["ai"]
    if provider == "offline":
        return {"ok": True, "message": "離線規則引擎（無需金鑰，離線可用）"}
    if provider in ("openai", "deepseek", "gemini", "claude") and not ai["keys"].get(provider):
        return {"ok": False, "error": "尚未設定該供應商的 API 金鑰"}
    if provider == "cloud" and not ai["cloud"].get("endpoint"):
        return {"ok": False, "error": "尚未設定雲端分析主機端點"}
    try:
        txt = call_provider(provider,
                            "You are a network diagnostics assistant. Reply with exactly: OK",
                            "Connectivity test. Reply OK.")
        return {"ok": True, "message": "連線成功", "sample": str(txt)[:80]}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:140]
        except Exception:
            body = ""
        return {"ok": False, "error": f"HTTP {e.code} {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:180]}
