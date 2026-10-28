"""统一的大语言 / 向量模型客户端（OpenAI 兼容协议）。

项目里原本有三处重复的 httpx 调用（正文撰写、智能推荐、每日简报），本模块把它们收敛为
一个客户端，并补充：重试与退避、超时、usage 统计、tool_calls / 旧式 function_call 归一化、
以及「端点是否支持 tools」的能力缓存。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.services.model_configs import get_active_endpoint

logger = get_logger("llm_client")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_RETRIES = 2

# 端点 id -> 是否支持原生 tools。仅进程内缓存，重启后重新探测。
_TOOL_SUPPORT: dict[str, bool] = {}


class LLMError(Exception):
    def __init__(self, message: str, *, code: str = "llm_error", status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LLMNotConfigured(LLMError):
    def __init__(self, message: str = "大语言模型未配置", code: str = "llm_not_configured"):
        super().__init__(message, code=code)


class ToolsUnsupported(LLMError):
    def __init__(self, message: str = "当前模型端点不支持原生工具调用"):
        super().__init__(message, code="tools_unsupported")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    model: str = ""


def normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def mark_tools_unsupported(endpoint_id: Any) -> None:
    if endpoint_id is not None:
        _TOOL_SUPPORT[str(endpoint_id)] = False


def protocol_for(endpoint: dict) -> str:
    """返回工具协议：native / json / none。

    - 显式配置优先（AGENT_TOOL_PROTOCOL=native|json|none）
    - auto：推理模型优先 JSON；已知不支持 tools 的端点降级 JSON；其余 native
    """
    raw = (read_env_file().get("AGENT_TOOL_PROTOCOL") or "auto").strip().lower()
    if raw in {"native", "json", "none"}:
        return raw
    model = (endpoint.get("model_id") or "").lower()
    if any(key in model for key in ("reasoner", "deepseek-r1", "thinking", "o1", "o3")):
        return "json"
    if _TOOL_SUPPORT.get(str(endpoint.get("id"))) is False:
        return "json"
    return "native"


def _looks_like_tool_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(key in lowered for key in ("tool", "function", "工具", "函数"))


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _parse_message(message: dict) -> tuple[str, list[ToolCall]]:
    content = _content_text(message.get("content"))
    calls: list[ToolCall] = []

    for index, item in enumerate(message.get("tool_calls") or []):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        raw_args = fn.get("arguments")
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{index}"),
                name=str(fn.get("name") or ""),
                arguments=_parse_arguments(raw_args),
                raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, ensure_ascii=False),
            )
        )

    # 旧式 function_call（部分兼容端点仍在使用）
    legacy = message.get("function_call")
    if isinstance(legacy, dict) and not calls:
        raw_args = legacy.get("arguments")
        calls.append(
            ToolCall(
                id="call_0",
                name=str(legacy.get("name") or ""),
                arguments=_parse_arguments(raw_args),
                raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, ensure_ascii=False),
            )
        )

    return content, calls


def _extract_usage(data: dict) -> dict[str, int]:
    usage = data.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


async def chat_endpoint(
    endpoint: dict,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    timeout: float = 60.0,
    response_format: dict | None = None,
    purpose: str = "chat",
    retries: int = DEFAULT_RETRIES,
    stream: bool = False,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    on_reasoning: Callable[[str], Awaitable[None]] | None = None,
) -> LLMResponse:
    """调用一个已解析的 LLM 端点。endpoint 来自 get_active_endpoint(db, "llm")。

    stream=True 时通过 on_delta（正文增量）与 on_reasoning（推理增量）实时回调。
    """
    base_url = normalize_base_url(endpoint.get("base_url", ""))
    api_key = endpoint.get("api_key") or ""
    model_id = endpoint.get("model_id") or ""
    if not base_url or not api_key or not model_id:
        raise LLMNotConfigured()

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if response_format:
        payload["response_format"] = response_format

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url}/chat/completions"

    if stream:
        return await _chat_stream(
            url,
            headers,
            payload,
            endpoint=endpoint,
            model_id=model_id,
            timeout=timeout,
            purpose=purpose,
            retries=retries,
            on_delta=on_delta,
            on_reasoning=on_reasoning,
        )

    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            raise LLMError(f"模型请求超时或网络错误：{exc}", code="llm_network") from exc

        if response.status_code >= 400:
            text = response.text[:500]
            if tools and response.status_code in (400, 422) and _looks_like_tool_error(text):
                mark_tools_unsupported(endpoint.get("id"))
                raise ToolsUnsupported(text)
            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                last_error = LLMError(text, code="llm_http", status_code=response.status_code)
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            try:
                detail = response.json().get("error", {})
                detail = detail.get("message") or detail or text
            except Exception:
                detail = text
            raise LLMError(
                f"模型调用失败（{response.status_code}）：{detail}",
                code="llm_http",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("模型返回了非 JSON 响应", code="llm_bad_response") from exc
        if not isinstance(data, dict):
            raise LLMError("模型返回了非预期的 JSON 结构", code="llm_bad_response")

        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        content, calls = _parse_message(message)
        usage = _extract_usage(data)
        result = LLMResponse(
            content=content,
            tool_calls=calls,
            usage=usage,
            finish_reason=str(choices[0].get("finish_reason") or ""),
            model=str(data.get("model") or model_id),
        )
        logger.info(
            "LLM 调用完成 purpose=%s model=%s tokens=%s tool_calls=%s",
            purpose,
            result.model,
            usage.get("total_tokens"),
            len(calls),
        )
        return result

    raise LLMError(f"模型调用失败：{last_error}", code="llm_retry_exhausted")


def _accumulate_tool_calls(store: dict[int, dict], raw_calls: list) -> None:
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        index = int(item.get("index") or 0)
        entry = store.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if item.get("id"):
            entry["id"] = item["id"]
        function = item.get("function") or {}
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]


async def _chat_stream(
    url: str,
    headers: dict,
    payload: dict,
    *,
    endpoint: dict,
    model_id: str,
    timeout: float,
    purpose: str,
    retries: int,
    on_delta: Callable[[str], Awaitable[None]] | None,
    on_reasoning: Callable[[str], Awaitable[None]] | None,
) -> LLMResponse:
    """流式调用（OpenAI SSE 协议）。返回完整响应，同时通过回调推送增量。"""
    payload = dict(payload)
    payload["stream"] = True

    for attempt in range(retries + 1):
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict[str, int] = {}
        finish_reason = ""
        model_name = model_id
        emitted = False

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")[:500]
                        if payload.get("tools") and response.status_code in (400, 422) and _looks_like_tool_error(body):
                            mark_tools_unsupported(endpoint.get("id"))
                            raise ToolsUnsupported(body)
                        if response.status_code in RETRYABLE_STATUS and attempt < retries:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        raise LLMError(
                            f"模型调用失败（{response.status_code}）：{body}",
                            code="llm_http",
                            status_code=response.status_code,
                        )

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if not isinstance(chunk, dict):
                            continue

                        if chunk.get("model"):
                            model_name = str(chunk["model"])
                        if chunk.get("usage"):
                            usage = _extract_usage(chunk)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0] or {}
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}

                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            emitted = True
                            reasoning_parts.append(str(reasoning))
                            if on_reasoning:
                                await on_reasoning(str(reasoning))

                        text = delta.get("content")
                        if text:
                            emitted = True
                            content_parts.append(str(text))
                            if on_delta:
                                await on_delta(str(text))

                        if delta.get("tool_calls"):
                            _accumulate_tool_calls(calls, delta["tool_calls"])
                        legacy = delta.get("function_call")
                        if isinstance(legacy, dict):
                            _accumulate_tool_calls(calls, [{"index": 0, "function": legacy}])
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if not emitted and attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            raise LLMError(f"模型流式请求超时或网络错误：{exc}", code="llm_network") from exc

        tool_calls = [
            ToolCall(
                id=entry["id"] or f"call_{index}",
                name=entry["name"],
                arguments=_parse_arguments(entry["arguments"]),
                raw_arguments=entry["arguments"],
            )
            for index, entry in sorted(calls.items())
            if entry["name"]
        ]

        if not usage.get("total_tokens"):
            # 部分兼容端点流式不返回 usage，做粗略估算用于预算统计
            completion = sum(len(part) for part in content_parts) + sum(len(part) for part in reasoning_parts)
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": completion,
                "total_tokens": completion,
            }

        result = LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=model_name,
        )
        logger.info(
            "LLM 流式完成 purpose=%s model=%s tokens=%s tool_calls=%s",
            purpose,
            result.model,
            usage.get("total_tokens"),
            len(tool_calls),
        )
        return result

    raise LLMError("模型流式调用失败", code="llm_retry_exhausted")


async def chat(
    db: AsyncSession,
    messages: list[dict[str, Any]],
    *,
    purpose: str = "chat",
    **kwargs: Any,
) -> LLMResponse:
    endpoint = await get_active_endpoint(db, "llm")
    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        raise LLMNotConfigured()
    return await chat_endpoint(endpoint, messages, purpose=purpose, **kwargs)


async def chat_text(
    db: AsyncSession,
    system_prompt: str,
    user_prompt: str,
    *,
    purpose: str = "text",
    **kwargs: Any,
) -> str:
    result = await chat(
        db,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        purpose=purpose,
        **kwargs,
    )
    return result.content


async def embed(db: AsyncSession, texts: list[str], *, timeout: float = 30.0) -> list[list[float]]:
    endpoint = await get_active_endpoint(db, "embedding")
    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        raise LLMNotConfigured("向量模型未配置", code="embedding_not_configured")
    if not texts:
        return []

    base_url = normalize_base_url(endpoint.get("base_url", ""))
    headers = {
        "Authorization": f"Bearer {endpoint.get('api_key', '')}",
        "Content-Type": "application/json",
    }
    payload = {"model": endpoint.get("model_id"), "input": texts}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base_url}/embeddings", headers=headers, json=payload)
    if response.status_code >= 400:
        raise LLMError(
            f"向量模型调用失败（{response.status_code}）：{response.text[:300]}",
            code="embedding_http",
            status_code=response.status_code,
        )
    data = response.json().get("data") or []
    data = sorted(data, key=lambda item: item.get("index", 0))
    return [list(item.get("embedding") or []) for item in data]
