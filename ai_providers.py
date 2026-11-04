"""
ai_providers.py — AI 分析引擎路由（NetInspect Pro）

設定頁的下拉選單選一個「供應商 + 模型」，填入該供應商的 API 金鑰後儲存，
AI 診斷與現場診斷的 AI 分析就改用它（config.ai.provider / models / keys）：
  · offline   ：離線規則引擎（免金鑰，由 recommendations / recommender 負責，本模組不呼叫）
  · codex     ：本機已登入的 Codex CLI（免金鑰；由 ai_advisor 呼叫）
  · openai    ：OpenAI Responses API
  · gemini    ：Google Gemini generateContent
  · deepseek  ：DeepSeek（OpenAI 相容 Chat Completions）
  · claude    ：Anthropic Messages API

只用標準庫 urllib，無額外相依。金鑰只從 config 讀取，永不寫入 log、不放進網址。
"""
import json
import shutil
import urllib.error
import urllib.request

import config

_TIMEOUT = 120      # 新一代模型會先思考，40 秒常常不夠

PROVIDER_NAMES = {
    "offline":  "離線規則引擎",
    "codex":    "Codex CLI",
    "openai":   "OpenAI",
    "gemini":   "Google Gemini",
    "deepseek": "DeepSeek",
    "claude":   "Anthropic Claude",
}
KEYED = ("openai", "gemini", "deepseek", "claude")   # 需要 API 金鑰的供應商

# 下拉選單的內建模型（2026-11 各家官方文件）。模型更新很快，
# 設定頁另有「從供應商載入模型清單」，用金鑰向供應商查詢實際可用的模型。
CATALOG = {
    "openai": [
        {"id": "gpt-6-astra",   "note": "建議・通用"},
        {"id": "gpt-6.1-sol",   "note": "旗艦"},
        {"id": "gpt-6.1-terra", "note": "快速"},
        {"id": "gpt-6-luna",    "note": "最省"},
    ],
    "gemini": [
        {"id": "gemini-3.8-flash",       "note": "建議"},
        {"id": "gemini-3.6-flash",       "note": "穩定版"},
        {"id": "gemini-3.5-flash-lite",  "note": "最省"},
        {"id": "gemini-3.1-pro-preview", "note": "Pro 預覽版"},
    ],
    "deepseek": [
        {"id": "deepseek-flash",  "note": "建議・V4.1 Flash"},
        {"id": "deepseek-v4-pro", "note": "V4 Pro"},
    ],
    "claude": [
        {"id": "claude-opus-5-5",   "note": "建議"},
        {"id": "claude-sonnet-5-5", "note": "較快較省"},
        {"id": "claude-haiku-5-5",  "note": "最省"},
        {"id": "claude-fable-5-1",  "note": "最強・最貴"},
    ],
}
DEFAULT_MODEL = {p: CATALOG[p][0]["id"] for p in CATALOG}

# 已停用或舊版的預設值 → 自動換成新預設（使用者自己填的其他模型不動）
LEGACY_MODELS = {
    "openai":   {"gpt-4o-mini", "gpt-4o"},
    "gemini":   {"gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash",
                 "gemini-2.5-flash", "gemini-2.5-pro"},
    "deepseek": {"deepseek-chat", "deepseek-reasoner"},
    "claude":   {"claude-sonnet-5"},
}


def codex_available() -> bool:
    return shutil.which("codex") is not None


def catalog() -> dict:
    """給設定頁的下拉選單。"""
    return {
        "providers": PROVIDER_NAMES,
        "keyed": list(KEYED),
        "models": CATALOG,
        "defaults": DEFAULT_MODEL,
        "codex_available": codex_available(),
    }


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _request(url: str, headers: dict, body: dict | None = None, timeout: int = _TIMEOUT) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = dict(headers)
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _openai(key: str, model: str, system: str, user: str) -> str:
    # 新一代模型只保證支援 Responses API；推理模型也不接受 temperature，故不送
    d = _request("https://api.openai.com/v1/responses",
                 {"Authorization": f"Bearer {key}"},
                 {"model": model, "instructions": system, "input": user})
    if isinstance(d.get("output_text"), str) and d["output_text"]:
        return d["output_text"]
    parts = [c.get("text", "")
             for item in d.get("output") or [] if item.get("type") == "message"
             for c in item.get("content") or [] if c.get("type") == "output_text"]
    if not any(parts):
        raise RuntimeError("OpenAI 沒有回傳文字（可能被拒答或輸出被截斷）")
    return "".join(parts)


def _deepseek(key: str, model: str, system: str, user: str) -> str:
    d = _request("https://api.deepseek.com/chat/completions",
                 {"Authorization": f"Bearer {key}"},
                 {"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]})
    return d["choices"][0]["message"]["content"]


def _gemini(key: str, model: str, system: str, user: str) -> str:
    # 金鑰放 header，不放網址（網址容易出現在錯誤訊息與代理伺服器記錄）
    d = _request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                 {"x-goog-api-key": key},
                 {"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": user}]}]})
    cands = d.get("candidates") or []
    if not cands:
        reason = (d.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"Gemini 沒有回傳結果{f'（{reason}）' if reason else ''}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise RuntimeError(f"Gemini 沒有回傳文字（{cands[0].get('finishReason', '未知原因')}）")
    return text


def _claude(key: str, model: str, system: str, user: str) -> str:
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    body = {"model": model, "max_tokens": 16000, "system": system,
            "messages": [{"role": "user", "content": user}]}
    # 安全機制拒答時，由伺服器自動改用建議的備援模型重跑（Haiku 不支援）
    if "haiku" not in model:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        body["fallbacks"] = "default"
    d = _request("https://api.anthropic.com/v1/messages", headers, body)
    if d.get("stop_reason") == "refusal":
        raise RuntimeError("Claude 拒絕回答這個請求")
    # 新一代模型的第一個區塊可能是思考區塊 → 只取文字區塊
    text = "".join(b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text")
    if not text:
        raise RuntimeError(f"Claude 沒有回傳文字（{d.get('stop_reason')}）")
    return text


_CALLERS = {"openai": _openai, "deepseek": _deepseek, "gemini": _gemini, "claude": _claude}


def model_for(provider: str, ai: dict | None = None) -> str:
    ai = ai or config.load()["ai"]
    return (ai.get("models") or {}).get(provider) or DEFAULT_MODEL.get(provider, "")


def call_provider(provider: str, system: str, user: str) -> str:
    """呼叫指定供應商，回傳文字。offline / codex 由上層處理，不應呼叫此函式。"""
    if provider not in _CALLERS:
        raise RuntimeError(f"「{PROVIDER_NAMES.get(provider, provider)}」不是線上 API 供應商")
    ai = config.load()["ai"]
    key = (ai.get("keys") or {}).get(provider)
    if not key:
        raise RuntimeError(f"尚未設定 {PROVIDER_NAMES[provider]} 的 API 金鑰")
    return _CALLERS[provider](key, model_for(provider, ai), system, user)


# ── 模型清單（用金鑰向供應商查詢）───────────────────────────────────────────────
_SKIP_OPENAI = ("realtime", "audio", "tts", "transcribe", "image", "search",
                "embedding", "moderation", "dall-e", "whisper", "codex")


def list_models(provider: str, key: str | None = None) -> list[str]:
    """回傳該供應商帳號實際能用的文字模型 ID（新的在前）。key 留空 = 用已儲存的金鑰。"""
    if provider not in _CALLERS:
        raise RuntimeError("這個選項不需要選模型")
    key = key or (config.load()["ai"].get("keys") or {}).get(provider)
    if not key:
        raise RuntimeError(f"請先填入 {PROVIDER_NAMES[provider]} 的 API 金鑰")
    if provider == "openai":
        d = _request("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}, timeout=20)
        items = sorted(d.get("data") or [], key=lambda m: m.get("created", 0), reverse=True)
        ids = [m["id"] for m in items if str(m.get("id", "")).startswith(("gpt-", "o"))
               and not any(s in m["id"] for s in _SKIP_OPENAI)]
    elif provider == "deepseek":
        d = _request("https://api.deepseek.com/models", {"Authorization": f"Bearer {key}"}, timeout=20)
        ids = [m["id"] for m in d.get("data") or [] if m.get("id")]
    elif provider == "gemini":
        d = _request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                     {"x-goog-api-key": key}, timeout=20)
        ids = [str(m.get("name", "")).removeprefix("models/") for m in d.get("models") or []
               if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        ids = [i for i in ids if i.startswith("gemini") and "embedding" not in i and "tts" not in i
               and "image" not in i]
        ids.reverse()          # API 依版本由舊到新列出
    else:
        d = _request("https://api.anthropic.com/v1/models?limit=100",
                     {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=20)
        ids = [m["id"] for m in d.get("data") or [] if m.get("id")]   # 已依發布時間新到舊
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:60]


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read().decode("utf-8", "replace"))
        msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        msg = msg or body.get("message") or ""
    except Exception:
        msg = ""
    hint = {401: "金鑰無效", 403: "金鑰沒有權限", 404: "找不到這個模型（名稱可能已更新）",
            429: "額度不足或請求太頻繁"}.get(e.code, "")
    return f"HTTP {e.code}{f' {hint}' if hint else ''}{f'：{str(msg)[:160]}' if msg else ''}"


def friendly_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return _http_error_text(e)
    if isinstance(e, urllib.error.URLError):
        return f"連不上供應商（{e.reason}）"
    return str(e)[:200]


def test(provider: str) -> dict:
    """測試目前選擇的引擎（設定頁「測試連線」）。金鑰缺失時給明確提示，不外洩金鑰。"""
    if provider == "offline":
        return {"ok": True, "message": "離線規則引擎（無需金鑰，離線可用）"}
    if provider == "codex":
        return ({"ok": True, "message": "已偵測到 Codex CLI"} if codex_available()
                else {"ok": False, "error": "找不到 Codex CLI（請先安裝並登入）"})
    if provider not in _CALLERS:
        return {"ok": False, "error": "不支援的供應商"}
    try:
        txt = call_provider(provider, "You are a network diagnostics assistant. Reply with exactly: OK",
                            "Connectivity test. Reply OK.")
        return {"ok": True, "message": f"連線成功（{model_for(provider)}）", "sample": str(txt)[:80]}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
