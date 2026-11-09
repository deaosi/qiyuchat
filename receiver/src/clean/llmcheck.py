"""模型网关联通性自检。

面向「自有网关」场景：只假设对方实现了 OpenAI 兼容的
`POST {base_url}/chat/completions`，其余能力（/models、response_format）一律按可选探测。
不通过也不抛异常，全部转成结构化结论，方便直接在浏览器/终端里看。
"""

from __future__ import annotations

import time
from typing import Any

from ..config import Settings
from ..http import HttpError, request

_PROBE_SYSTEM = "你是联通性测试助手，只按要求输出，不要解释。"
_PROBE_USER = "只回复两个字：正常"


def _mask(key: str) -> str:
    if not key:
        return "未配置（部分内网网关可免鉴权）"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:5]}…{key[-4:]}（{len(key)} 位）"


def _headers(cfg: Settings) -> dict[str, str]:
    headers: dict[str, str] = {}
    if cfg.llm.api_key:
        headers["Authorization"] = f"Bearer {cfg.llm.api_key}"
    for key, value in cfg.llm.extra_headers:
        headers[key] = value
    return headers


def _probe_models(cfg: Settings) -> dict[str, Any]:
    url = cfg.llm.base_url.rstrip("/") + "/models"
    t0 = time.perf_counter()
    try:
        resp = request("GET", url, headers=_headers(cfg), timeout=min(cfg.llm.timeout, 20), retries=1)
        items = resp.get("data") if isinstance(resp, dict) else None
        names = [str(x.get("id")) for x in items or [] if isinstance(x, dict) and x.get("id")]
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000),
                "count": len(names), "models": names[:30]}
    except HttpError as e:
        if e.status == 404:
            return {"ok": False, "optional": True, "ms": round((time.perf_counter() - t0) * 1000),
                    "error": "网关没有 /models 接口（可选能力，不影响调用）"}
        return {"ok": False, "optional": True, "ms": round((time.perf_counter() - t0) * 1000),
                "error": f"HTTP {e.status}：{(e.body or '')[:160]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "optional": True,
                "ms": round((time.perf_counter() - t0) * 1000), "error": str(e)[:160]}


def _probe_chat(cfg: Settings, model: str, json_mode: bool) -> dict[str, Any]:
    url = cfg.llm.base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PROBE_SYSTEM},
            {"role": "user", "content": _PROBE_USER if not json_mode else '只输出 JSON：{"ok": true}'},
        ],
        "temperature": 0,
        "stream": False,
        "max_tokens": 64,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    t0 = time.perf_counter()
    try:
        resp = request("POST", url, body=payload, headers=_headers(cfg),
                       timeout=cfg.llm.timeout, retries=1)
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = resp.get("usage") or {}
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000),
                "model": resp.get("model") or model,
                "reply": (text or "").strip()[:120],
                "tokens": usage.get("total_tokens")}
    except HttpError as e:
        hint = ""
        if e.status in (401, 403):
            hint = "鉴权失败：检查 LLM_API_KEY / LLM_EXTRA_HEADERS"
        elif e.status == 404:
            hint = "路径不对：LLM_BASE_URL 通常要以 /v1 结尾"
        elif e.status == 400:
            hint = "参数被拒：可能是模型名不存在，或网关不支持 response_format"
        return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000),
                "error": f"HTTP {e.status} {(e.body or '')[:200]}", "hint": hint}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000),
                "error": str(e)[:200],
                "hint": "连不上：检查出网、DNS、网关地址与端口"}


def check(cfg: Settings, model: str = "") -> dict[str, Any]:
    """跑三项探测，返回结构化结论。"""
    target = model or cfg.llm.model_main or cfg.llm.model_fast
    out: dict[str, Any] = {
        "base_url": cfg.llm.base_url,
        "api_key": _mask(cfg.llm.api_key),
        "extra_headers": [k for k, _ in cfg.llm.extra_headers],
        "model": target,
        "dry_run": cfg.llm.dry_run,
        "timeout": cfg.llm.timeout,
    }
    if not cfg.llm.base_url.strip():
        out["verdict"] = "未配置"
        out["note"] = "LLM_BASE_URL 为空：填上网关根路径（一般以 /v1 结尾）再跑一次"
        return out
    if cfg.llm.dry_run:
        out["verdict"] = "跳过"
        out["note"] = "LLM_DRY_RUN=1，当前是假数据模式；改 0 才会真调网关"
        return out

    out["models"] = _probe_models(cfg)
    out["chat"] = _probe_chat(cfg, target, json_mode=False)
    if out["chat"].get("ok") and cfg.llm.json_mode:
        out["json_mode"] = _probe_chat(cfg, target, json_mode=True)
    out["verdict"] = "可用" if out["chat"].get("ok") else "不可用"
    return out


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"网关地址   {result['base_url']}",
        f"API Key    {result['api_key']}",
        f"额外请求头 {', '.join(result['extra_headers']) or '无'}",
        f"模型       {result['model']}",
        f"超时       {result['timeout']}s",
    ]
    if result.get("note"):
        lines.append(f"提示       {result['note']}")
    models = result.get("models")
    if models:
        if models.get("ok"):
            lines.append(f"/models    OK  {models['count']} 个模型  {models['ms']}ms")
            if models.get("models"):
                lines.append("           " + ", ".join(models["models"][:8]))
        else:
            lines.append(f"/models    跳过  {models.get('error', '')}")
    chat = result.get("chat")
    if chat:
        if chat.get("ok"):
            lines.append(f"对话测试   OK  {chat['ms']}ms  回复：{chat.get('reply', '')!r}")
        else:
            lines.append(f"对话测试   失败  {chat.get('error', '')}")
            if chat.get("hint"):
                lines.append(f"           建议：{chat['hint']}")
    js = result.get("json_mode")
    if js:
        if js.get("ok"):
            lines.append(f"JSON 模式  OK  {js['ms']}ms")
        else:
            lines.append("JSON 模式  不支持 —— 把 LLM_JSON_MODE 设为 0，改用提示词约束")
    lines.append(f"结论       {result['verdict']}")
    return "\n".join(lines)
