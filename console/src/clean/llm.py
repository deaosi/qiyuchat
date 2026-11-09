"""1/2/3 档模型路由。

所有 provider 都走 OpenAI 兼容协议（DeepSeek / Qwen / GLM / vLLM / Ollama / Azure 兼容层），
换模型只改 .env 里的 LLM_BASE_URL + LLM_MODEL_*，代码不动。

LLM_DRY_RUN=1 时不发请求，用确定性伪结果，方便本地把整条链路跑通。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from ..config import LLMConfig
from ..http import HttpError, request
from ..log import get
from . import prompts as P

log = get("llm")


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    """模型偶尔会包 ```json 或加前后废话，这里做容错提取。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        s, e = text.find(open_ch), text.rfind(close_ch)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"无法从模型输出解析 JSON: {text[:200]}")


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    # ---------- 底层 ----------

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        for key, value in self.cfg.extra_headers:
            headers[key] = value
        return headers

    def _payload(self, model: str, system: str, user: str, use_json: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def chat(self, system: str, user: str, model: str, json_mode: bool = True,
             retries: int = 3) -> str:
        if self.cfg.dry_run:
            return self._fake(system, user)

        if not self.cfg.base_url.strip():
            raise LLMError("未配置 LLM_BASE_URL：请在 .env 里填网关根路径（一般以 /v1 结尾）")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = self._headers()
        use_json = json_mode and self.cfg.json_mode

        last: Exception | None = None
        attempt = 0
        while attempt < retries:
            payload = self._payload(model, system, user, use_json)
            try:
                resp = request("POST", url, body=payload, headers=headers,
                               timeout=self.cfg.timeout, retries=1)
                return resp["choices"][0]["message"]["content"]
            except HttpError as e:
                last = e
                # 网关不认 response_format：降级重试一次，别把整条链路卡死
                if use_json and e.status in (400, 422) and "response_format" in (e.body or ""):
                    log.warning("网关不支持 response_format，降级为提示词约束 JSON")
                    use_json = False
                    continue
                if e.status in (401, 403):
                    raise LLMError(
                        f"模型网关拒绝鉴权（HTTP {e.status}）：检查 LLM_API_KEY / LLM_EXTRA_HEADERS。"
                        f"原始响应：{(e.body or '')[:200]}") from e
                if e.status == 404:
                    raise LLMError(
                        f"接口不存在（HTTP 404）：LLM_BASE_URL 应为 OpenAI 兼容根路径，"
                        f"实际请求 {url}。多数网关需要以 /v1 结尾。") from e
            except Exception as e:                      # noqa: BLE001
                last = e
            attempt += 1
            if attempt < retries:
                time.sleep(2 ** attempt)
        raise LLMError(f"模型调用失败 model={model}: {last}")

    def _fake(self, system: str, user: str) -> str:
        """DRY RUN：用内容哈希生成稳定伪结果，保证多次运行一致、可测。"""
        h = int(hashlib.md5(user.encode()).hexdigest()[:8], 16)
        cats = list(P.CATEGORIES)
        if "分类器" in system:
            n = max(1, user.count("### 会话"))
            out = []
            for i in range(n):
                c = cats[(h + i) % len(cats)]
                subs = P.CATEGORIES[c]
                out.append({
                    "category": c,
                    "sub": subs[(h + i) % len(subs)],
                    "emotion": ["calm", "annoyed", "angry"][(h + i) % 3],
                    "resolved": (h + i) % 2 == 0,
                    "risk": ["low", "mid", "high"][(h + i) % 3],
                    "confidence": round(0.6 + ((h + i) % 40) / 100, 2),
                })
            return json.dumps(out, ensure_ascii=False)
        return json.dumps({
            "demand": "[DRY RUN] 客户反馈问题",
            "result": "[DRY RUN] 客服已受理",
            "product": "",
            "followup": h % 2 == 0,
            "one_line": "[DRY RUN] 摘要占位",
            "confidence": 0.7,
        }, ensure_ascii=False)

    # ---------- 业务方法 ----------

    def classify(self, texts: Sequence[str]) -> list[dict[str, Any]]:
        """批量分类。返回与 texts 等长的列表，失败位置补空 dict。"""
        if not texts:
            return []
        model = self.cfg.model_fast
        out: list[dict[str, Any]] = []
        for i in range(0, len(texts), self.cfg.batch_size):
            chunk = list(texts[i:i + self.cfg.batch_size])
            try:
                raw = self.chat(P.CLASSIFY_SYSTEM, P.classify_user_prompt(chunk), model)
                parsed = _extract_json(raw)
                if isinstance(parsed, dict):
                    parsed = [parsed]
                parsed = list(parsed)[:len(chunk)]
                while len(parsed) < len(chunk):
                    parsed.append({})
                out.extend(parsed)
            except Exception as e:                      # noqa: BLE001
                log.warning("分类批次失败(%d 条)：%s", len(chunk), e)
                out.extend([{} for _ in chunk])
        return out

    def summarize(self, text: str) -> dict[str, Any]:
        model = self.cfg.model_main
        try:
            raw = self.chat(P.SUMMARY_SYSTEM, P.summary_user_prompt(text), model)
            data = _extract_json(raw)
            return data if isinstance(data, dict) else {}
        except Exception as e:                          # noqa: BLE001
            log.warning("摘要失败：%s", e)
            return {}

    def escalate(self, text: str) -> dict[str, Any]:
        """3 档：难例升级到更强模型。没配就复用 main。"""
        model = self.cfg.model_hard or self.cfg.model_main
        if model == self.cfg.model_main:
            return self.summarize(text)
        try:
            raw = self.chat(P.SUMMARY_SYSTEM, P.summary_user_prompt(text), model)
            data = _extract_json(raw)
            return data if isinstance(data, dict) else {}
        except Exception as e:                          # noqa: BLE001
            log.warning("升级摘要失败：%s", e)
            return {}

    def map(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        """并发执行，保持顺序。"""
        if self.cfg.concurrency <= 1 or len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as ex:
            return list(ex.map(fn, items))
