"""
ai_advisor.py — AI 建議行動引擎
把一鍵診斷的所有量測結果整理成精簡 JSON，交給 AI 分析，
輸出「給經驗不足的 IT 工程師」看得懂、可立即執行的三級行動清單。

Provider 鏈（自動降級）：
  1. Codex CLI（OpenAI OAuth，`codex exec` headless + --output-schema 強制 JSON）
  2. 離線規則引擎（recommender.py 的 full_assessment 映射成相同格式）

優先級：immediate（🔴 立即處理）/ adjust（🟡 建議調整）/ ok（🟢 狀態正常）
"""
import json
import os
import shutil
import subprocess
import tempfile

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


def _codex_analyze(payload: dict) -> dict | None:
    codex = shutil.which("codex")
    if not codex:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        out_path    = os.path.join(tmp, "out.json")
        with open(schema_path, "w") as f:
            json.dump(_SCHEMA, f)
        prompt = _PROMPT + json.dumps(payload, ensure_ascii=False, indent=1)
        try:
            subprocess.run(
                [codex, "exec",
                 "--sandbox", "read-only",
                 "--skip-git-repo-check",
                 "-C", tmp,
                 "--output-schema", schema_path,
                 "--output-last-message", out_path,
                 "-"],
                input=prompt, text=True, capture_output=True,
                timeout=CODEX_TIMEOUT,
            )
            with open(out_path) as f:
                result = json.load(f)
        except Exception:
            return None
    if not isinstance(result.get("items"), list) or not result["items"]:
        return None
    return result


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

    result = _codex_analyze(payload)
    provider = "codex"
    if result is None:
        try:
            result = _rules_analyze(networks, analysis, current, netperf)
            provider = "rules"
        except Exception as e:
            return {"ok": False, "provider": "none", "error": str(e),
                    "summary": "", "score": 0, "items": []}

    # 排序：immediate → adjust → ok
    order = {"immediate": 0, "adjust": 1, "ok": 2}
    result["items"].sort(key=lambda i: order.get(i.get("priority"), 9))
    return {"ok": True, "provider": provider, **result}
