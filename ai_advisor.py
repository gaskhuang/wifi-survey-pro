"""
ai_advisor.py — AI 建議行動引擎
把一鍵診斷的所有量測結果整理成精簡 JSON，交給 AI 分析，
輸出「給經驗不足的 IT 工程師」看得懂、可立即執行的三級行動清單。

使用設定頁選擇的引擎（config.ai.provider），失敗時降級為離線規則引擎：
  · openai / gemini / deepseek / claude：用使用者填的 API 金鑰呼叫（ai_providers）
  · codex：本機已登入的 Codex CLI（`codex exec` headless + --output-schema 強制 JSON）
  · offline：離線規則引擎（recommender.py 的 full_assessment 映射成相同格式）

優先級：immediate（🔴 立即處理）/ adjust（🟡 建議調整）/ ok（🟢 狀態正常）
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile

import ai_providers
import config
import recommender

CODEX_TIMEOUT = 180

# ── 輸出 JSON Schema（餵給 codex --output-schema）─────────────────────────────
_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "一句話總結現場 Wi-Fi 狀況（繁體中文）"},
        "score":   {"type": "integer",
                    "description": "整體健康分數 0-100"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "string",
                                 "enum": ["immediate", "adjust", "ok"]},
                    "problem":  {"type": "string"},
                    "action":   {"type": "string"},
                    "category": {"type": "string",
                                 "enum": ["頻道", "訊號", "速度", "穩定度",
                                          "安全", "設備", "其他"]},
                },
                "required": ["priority", "problem", "action", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "score", "items"],
    "additionalProperties": False,
}

_PROMPT = """你是資深 Wi-Fi 無線網路顧問，在幫一位「經驗不足的年輕 IT 工程師」看現場診斷數據。
他現在人在客戶現場，需要馬上知道下一步該做什麼。

以下是他筆電量到的完整診斷資料（JSON）。請分析並輸出行動建議。

要求：
1. 語言淺顯白話（繁體中文），專業術語第一次出現時用括號簡單解釋
2. 每個問題一條，problem 要引用具體數值（例如「頻道 6 有 4 個 AP 搶用」）
3. action 是現場可立即執行的具體步驟（例如「登入 AP 管理介面，把 2.4G 頻道改成 1 或 11」），不要空泛的「建議優化」
4. priority 三級：immediate = 會直接影響使用、今天就要處理；adjust = 有改善空間、排程處理；ok = 正常、讓工程師安心的確認項
5. 重要：要做「跨指標推理」——例如訊號很強但延遲抖動大，要指出可能是 AP 過載或省電模式，而不是叫他改善訊號
6. ok 項目至少列 2 條（讓工程師知道哪些不用動），總項目 5-10 條
7. 若資料中某區塊缺失（null），跳過即可，不要編造

診斷資料：
"""


# ── Payload builder ──────────────────────────────────────────────────────────
def build_payload(networks: list, analysis: dict, current: dict,
                  netperf: dict, speedtest: dict | None,
                  stability_result: dict | None) -> dict:
    """把各模組結果壓縮成 < 4KB 的精簡 JSON，省 token 也降雜訊。"""

    # 環境概況：各頻段 AP 數 + 每頻道佔用 top5
    by_band: dict = {}
    ch_count: dict = {}
    for n in networks:
        band = n.get("band", "?")
        by_band[band] = by_band.get(band, 0) + 1
        key = f"{band} ch{n.get('channel')}"
        ch_count[key] = ch_count.get(key, 0) + 1
    top_channels = sorted(ch_count.items(), key=lambda kv: -kv[1])[:5]

    # 目前頻道有幾個鄰居搶用
    cur_ch, cur_band = current.get("channel"), current.get("band")
    same_ch = sum(1 for n in networks
                  if n.get("channel") == cur_ch and n.get("band") == cur_band
                  and n.get("bssid") != current.get("bssid"))

    best = {}
    for band in ("2.4GHz", "5GHz", "6GHz"):
        bc = (analysis.get(band) or {}).get("best_channels") or []
        if bc:
            best[band] = [b.get("channel") for b in bc[:3]]

    payload = {
        "current_connection": {
            "ssid":     current.get("ssid"),
            "band":     cur_band,
            "channel":  cur_ch,
            "width_mhz": current.get("width"),
            "rssi_dbm": current.get("rssi"),
            "snr_db":   current.get("snr"),
            "tx_rate_mbps": current.get("tx_rate"),
            "standard": current.get("standard"),
            "neighbors_on_same_channel": same_ch,
        } if current.get("connected") else None,
        "environment": {
            "total_aps":     len(networks),
            "aps_by_band":   by_band,
            "busiest_channels": [{"ch": k, "aps": v} for k, v in top_channels],
            "recommended_clean_channels": best or None,
            "open_unsecured_aps": sum(1 for n in networks
                                      if "Open" in str(n.get("security", ""))),
        },
        "latency_test": {
            "ping_ms":        netperf.get("ping_ms"),
            "ping_jitter_ms": netperf.get("ping_jitter_ms"),
            "packet_loss_pct": netperf.get("ping_loss_pct"),
            "dns_ms":         netperf.get("dns_ms"),
        } if netperf else None,
        "speedtest": {
            "download_mbps": speedtest.get("down_mbps"),
            "upload_mbps":   speedtest.get("up_mbps"),
            "http_latency_ms": speedtest.get("latency_ms"),
        } if speedtest and speedtest.get("ok") else None,
    }

    if stability_result:
        r = stability_result
        payload["stability_60s_test"] = {
            "duration_s":  r.get("duration"),
            "rssi_mean":   (r.get("rssi") or {}).get("mean"),
            "rssi_stddev": (r.get("rssi") or {}).get("std"),
            "snr_mean":    (r.get("snr") or {}).get("mean"),
            "ping_gateway":  _ping_brief(r.get("ping_gw")),
            "ping_internet": _ping_brief(r.get("ping_inet")),
            "roam_count":       r.get("roam_count"),
            "disconnect_count": r.get("disconnect_count"),
        }
    return payload


def _ping_brief(p: dict | None) -> dict | None:
    if not p or not p.get("sent"):
        return None
    return {"avg_ms": p.get("avg_ms"), "jitter_ms": p.get("jitter_ms"),
            "loss_pct": p.get("loss_pct")}


# ── Provider 1: Codex CLI（OpenAI OAuth）─────────────────────────────────────
def codex_available() -> bool:
    return shutil.which("codex") is not None


def _kill_tree(proc: subprocess.Popen):
    """codex 是 node shim → 原生子程序；只殺 shim 會留下孤兒繼續耗額度。
    以 session（process group）為單位：先 SIGTERM，3 秒後仍在就 SIGKILL。"""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)   # 連同已脫離 shim 的子孫一起收掉
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.kill()
        proc.communicate(timeout=1)   # 收回管線與殭屍程序
    except Exception:
        pass


def _run_codex(cmd: list, prompt: str, timeout: float) -> bool:
    """執行 codex；逾時則終止整個程序群組。回傳是否在時限內結束。"""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        proc.communicate(input=prompt, timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return False
    except BaseException:
        _kill_tree(proc)
        raise


def _validate(result) -> dict | None:
    """Codex 輸出驗證：必須是 dict 且 items 為非空的 dict 清單，否則交給規則引擎。"""
    if not isinstance(result, dict):
        return None
    items = result.get("items")
    if not isinstance(items, list):
        return None
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return None
    score = result.get("score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 0
    summary = result.get("summary")
    return {**result, "items": items, "score": score,
            "summary": summary if isinstance(summary, str) else ""}


def _codex_analyze(payload: dict) -> dict | None:
    codex = shutil.which("codex")
    if not codex:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = os.path.join(tmp, "schema.json")
            out_path    = os.path.join(tmp, "out.json")
            with open(schema_path, "w") as f:
                json.dump(_SCHEMA, f)
            prompt = _PROMPT + json.dumps(payload, ensure_ascii=False, indent=1)
            if not _run_codex(
                    [codex, "exec",
                     "--sandbox", "read-only",
                     "--skip-git-repo-check",
                     "-C", tmp,
                     "--output-schema", schema_path,
                     "--output-last-message", out_path,
                     "-"],
                    prompt, CODEX_TIMEOUT):
                return None
            with open(out_path) as f:
                result = json.load(f)
        return _validate(result)
    except Exception:
        return None


def _extract_json(text: str):
    """從模型回覆取出 JSON（容忍 ```json 區塊或前後多餘文字）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(t[a:b + 1])
    except ValueError:
        return None


def _api_analyze(provider: str, payload: dict) -> dict | None:
    """用設定頁選的雲端模型分析；回覆不是合法 JSON 就交給規則引擎。"""
    try:
        system = ("你是資深 Wi-Fi 無線網路顧問。只輸出一個 JSON 物件，不要任何其他文字或 Markdown，"
                  "格式必須符合這個 JSON Schema：\n" + json.dumps(_SCHEMA, ensure_ascii=False))
        text = ai_providers.call_provider(provider, system,
                                          _PROMPT + json.dumps(payload, ensure_ascii=False, indent=1))
        return _validate(_extract_json(text))
    except Exception:
        return None


def current_engine() -> str:
    """實際會用的引擎：選了雲端模型但沒金鑰、選了 Codex 但沒安裝 → 退回 rules。"""
    ai = config.load()["ai"]
    p = ai.get("provider", "offline")
    if p in ai_providers.KEYED and (ai.get("keys") or {}).get(p):
        return p
    if p == "codex" and codex_available():
        return "codex"
    return "rules"


def engine_label(engine: str | None = None) -> str:
    engine = engine or current_engine()
    if engine == "rules":
        return "離線規則引擎"
    if engine in ai_providers.KEYED:
        return f"{ai_providers.PROVIDER_NAMES[engine]}・{ai_providers.model_for(engine)}"
    return ai_providers.PROVIDER_NAMES.get(engine, engine)


def codex_text(prompt: str, timeout: float = CODEX_TIMEOUT) -> str:
    """Codex CLI 純文字回覆（AI 診斷的深入分析用）。"""
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("找不到 Codex CLI")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "out.txt")
        if not _run_codex([codex, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                           "-C", tmp, "--output-last-message", out_path, "-"], prompt, timeout):
            raise RuntimeError("Codex 逾時")
        with open(out_path, encoding="utf-8") as f:
            return f.read()


# ── Provider 2: 離線規則引擎（recommender 映射）──────────────────────────────
_SEV_TO_PRIORITY = {"critical": "immediate", "warning": "adjust",
                    "info": "adjust", "good": "ok"}

def _rules_analyze(networks, analysis, current, netperf) -> dict:
    assessment = recommender.full_assessment(
        networks, analysis, current, netperf, [])
    items = []
    for issue in assessment.get("issues", []):
        items.append({
            "priority": _SEV_TO_PRIORITY.get(issue.get("severity"), "adjust"),
            "problem":  issue.get("title", "") +
                        (f"：{issue['problem']}" if issue.get("problem") else ""),
            "action":   issue.get("recommendation", ""),
            "category": issue.get("category", "其他"),
        })
    grade = assessment.get("grade", {})
    return {
        "summary": f"離線規則引擎評估：整體 {assessment.get('score', 0)} 分"
                   f"（{grade.get('label', '')}）",
        "score": assessment.get("score", 0),
        "items": items,
    }


# ── Public API ───────────────────────────────────────────────────────────────
def analyze(networks: list, analysis: dict, current: dict, netperf: dict,
            speedtest: dict | None = None,
            stability_result: dict | None = None) -> dict:
    """回傳 {ok, provider, summary, score, items}。永不拋例外。"""
    payload = build_payload(networks, analysis, current, netperf,
                            speedtest, stability_result)

    provider = current_engine()
    result = None
    if provider == "codex":
        result = _codex_analyze(payload)
    elif provider in ai_providers.KEYED:
        result = _api_analyze(provider, payload)
    if result is None:
        try:
            result = _rules_analyze(networks, analysis, current, netperf)
            provider = "rules"
        except Exception as e:
            return {"ok": False, "provider": "none", "error": str(e),
                    "summary": "", "score": 0, "items": []}

    # 排序：immediate → adjust → ok
    order = {"immediate": 0, "adjust": 1, "ok": 2}
    result["items"].sort(key=lambda i: order.get(i.get("priority"), 9)
                         if isinstance(i, dict) else 9)
    return {"ok": True, "provider": provider, "provider_label": engine_label(provider), **result}
