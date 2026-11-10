"""Model-neutral structured completion contract for the VOC data assistant.

Business schemas belong to callers. Provider adapters normalize transport and
output envelopes, never infer capabilities from model-name prefixes.
"""
from __future__ import annotations

import json
import socket
import urllib.error
from typing import Callable, Protocol

from .config import LLMConfig
from .voc_model import VocModelError, _gateway, _parse_output


class ModelProtocolError(ValueError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class CompletionAdapter(Protocol):
    def complete(self, llm: LLMConfig, system: str, payload: dict, max_tokens: int) -> dict: ...


class OpenAICompatibleAdapter:
    def complete(self, llm: LLMConfig, system: str, payload: dict, max_tokens: int) -> dict:
        data=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e')
        user=f'{system}\n\n<untrusted_data>\n{data}\n</untrusted_data>\n\n{system}'
        request = {"model": llm.model_main, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}],
            "temperature": 0, "stream": False, "max_tokens": max_tokens}
        if llm.json_mode:
            request["response_format"] = {"type": "json_object"}
        return _gateway(llm, request)


_ADAPTERS: dict[str, Callable[[], CompletionAdapter]] = {"openai-compatible": OpenAICompatibleAdapter}


def register_adapter(name: str, factory: Callable[[], CompletionAdapter]) -> None:
    """Server-side extension point; never populated from model output or HTTP."""
    if not name or not callable(factory):
        raise ValueError("Invalid model adapter")
    _ADAPTERS[name] = factory


def _timeout(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (TimeoutError, socket.timeout)):
            return True
        if isinstance(error, urllib.error.URLError) and isinstance(error.reason, (TimeoutError, socket.timeout)):
            return True
        error = error.__cause__ or error.__context__
    return False


def complete_json(llm: LLMConfig, system: str, payload: dict, max_tokens: int = 2400,
                  *, adapter: str = "openai-compatible") -> dict:
    if llm.dry_run or not llm.base_url or not llm.api_key or not llm.model_main:
        raise ModelProtocolError("模型服务尚未启用，请先配置模型网关。", 400)
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 24000 or len(system) > 5000:
        raise ModelProtocolError("本次检索内容过多，请缩小范围后重试。", 400)
    if type(max_tokens) is not int or not 64 <= max_tokens <= 4096:
        raise ModelProtocolError("模型输出预算无效。", 400)
    factory = _ADAPTERS.get(adapter)
    if factory is None:
        raise ModelProtocolError("当前模型协议尚未配置适配器。", 400)
    try:
        response = factory().complete(llm, system, payload, max_tokens)
        if not isinstance(response, dict):
            raise ModelProtocolError("模型返回的响应格式不兼容。")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelProtocolError("模型未返回可读取的回答。")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content
                              if isinstance(item, dict) and isinstance(item.get("text"), str))
        data = _parse_output(content)
        if not isinstance(data, dict):
            raise ModelProtocolError("模型未按约定返回结构化对象。")
        usage = response.get("usage")
        tokens = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
        if type(tokens) is not int or tokens < 0:
            tokens = 0
        return {"data": data, "usage_tokens": tokens, "model": llm.model_main}
    except ModelProtocolError:
        raise
    except Exception as error:
        if _timeout(error):
            raise ModelProtocolError(f"模型响应超过 {llm.timeout} 秒，请缩小查询范围或调整等待时间。", 504) from None
        if isinstance(error, VocModelError):
            raise ModelProtocolError(str(error),504 if getattr(error,'status',None)==504 else 502) from None
        raise ModelProtocolError("模型服务调用失败，请检查网关或稍后重试。") from None
