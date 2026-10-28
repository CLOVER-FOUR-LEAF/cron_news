"""资讯助手 Agent 运行时：工具循环、上下文预算、引用校验。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.services import conversation_service, llm_client, retrieval
from app.services.agent import tools as tool_registry
from app.services.agent_prompts import get_agent_prompt
from app.services.model_configs import get_active_endpoint

logger = get_logger("agent.runtime")

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

MAX_TOKENS_PER_TURN = 1200
OUTPUT_RESERVE = 2000

_daily_usage = {"date": "", "tokens": 0}

CITATION_RE = re.compile(r"\[\[?\s*(news|web)\s*[:#]\s*([^\]\n]{1,60}?)\s*\]\]?")
# 流式场景只认完整的双括号标记，避免单括号被切分时提前替换
STREAM_CITATION_RE = re.compile(r"\[\[\s*(news|web)\s*[:#]\s*([^\]\n]{1,60}?)\s*\]\]")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(read_env_file().get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def load_config() -> dict[str, Any]:
    env = read_env_file()
    return {
        "enabled": (env.get("AGENT_CHAT_ENABLED", "true") or "true").lower() != "false",
        "max_iterations": _env_int("AGENT_MAX_ITERATIONS", 5, 1, 10),
        "max_tool_calls": _env_int("AGENT_MAX_TOOL_CALLS", 12, 1, 30),
        "context_tokens": _env_int("AGENT_CONTEXT_TOKENS", 12000, 2000, 60000),
        "history_messages": _env_int("AGENT_HISTORY_MESSAGES", 12, 0, 50),
        "daily_budget": _env_int("AGENT_DAILY_TOKEN_BUDGET", 300000, 1000, 10_000_000),
        "llm_timeout": 90.0,
    }


def _today_key() -> str:
    return date.today().isoformat()


def daily_tokens_used() -> int:
    if _daily_usage["date"] != _today_key():
        _daily_usage["date"] = _today_key()
        _daily_usage["tokens"] = 0
    return int(_daily_usage["tokens"])


def _record_usage(usage: dict[str, int]) -> None:
    if _daily_usage["date"] != _today_key():
        _daily_usage["date"] = _today_key()
        _daily_usage["tokens"] = 0
    _daily_usage["tokens"] += int(usage.get("total_tokens") or 0)


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)


def _estimate_tokens(text: str) -> int:
    total = 4.0
    for char in text or "":
        total += 1.0 if ord(char) > 127 else 0.25
    return int(total)


def _remove_tool_group(messages: list[dict[str, Any]], index: int) -> None:
    """删除 assistant tool_calls 消息及其对应的 tool 结果，避免留下孤立工具消息。"""
    message = messages[index]
    tool_ids = {
        call.get("id")
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict)
    }
    messages.pop(index)
    if not tool_ids:
        return
    remaining = []
    for item in messages[index:]:
        if item.get("role") == "tool" and item.get("tool_call_id") in tool_ids:
            continue
        remaining.append(item)
    messages[index:] = remaining


def _fit_messages(messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """超预算时优先丢弃最旧的工具调用组，再丢最旧消息；system 与最新用户消息永不丢。"""

    def total() -> int:
        return sum(_estimate_tokens(str(message.get("content") or "")) + 8 for message in messages)

    last_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=None,
    )

    while total() > budget and len(messages) > 2:
        group_index = next(
            (
                index
                for index, message in enumerate(messages)
                if index != last_user
                and message.get("role") == "assistant"
                and message.get("tool_calls")
            ),
            None,
        )
        if group_index is not None:
            _remove_tool_group(messages, group_index)
            last_user = max(
                (index for index, message in enumerate(messages) if message.get("role") == "user"),
                default=None,
            )
            continue

        candidates = [
            index
            for index in range(1, len(messages))
            if index != last_user and messages[index].get("role") != "system"
        ]
        if not candidates:
            break
        remove_at = candidates[0]
        messages.pop(remove_at)
        if last_user is not None and remove_at < last_user:
            last_user -= 1

    last_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=None,
    )
    guard = 0
    while total() > budget and guard < 20:
        guard += 1
        candidates = [
            index
            for index in range(1, len(messages))
            if messages[index].get("role") != "system" and index != last_user
        ]
        if not candidates:
            break
        index = max(candidates, key=lambda i: len(str(messages[i].get("content") or "")))
        content = str(messages[index].get("content") or "")
        if len(content) <= 600:
            break
        messages[index]["content"] = content[: max(600, len(content) // 2)] + "…（已截断）"
    return messages


async def _build_system_prompt(db: AsyncSession) -> str:
    raw = await get_agent_prompt(db, "chat")
    nickname = read_env_file().get("NICKNAME") or "用户"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = raw.replace("{now}", now).replace("{nickname}", nickname)
    # 协作约定固定追加：即使管理员改过提示词，委派能力也不会丢失
    return prompt + (
        "\n\n【团队协作】用户要求采集新闻、生成简报、更新相关推荐时，"
        "分别调用 delegate_collect / delegate_brief / delegate_recommend，"
        "完成后基于工具返回的真实结果汇报，不要回答「这不是我的工作」。"
    )


def _json_instruction() -> str:
    lines = []
    for schema in tool_registry.tool_schemas():
        fn = schema["function"]
        params = json.dumps(fn.get("parameters") or {}, ensure_ascii=False)
        lines.append(f"- {fn['name']}: {fn['description']} 参数：{params}")
    tools_text = "\n".join(lines)
    return (
        "\n\n【工具调用协议】本轮你可以使用以下只读工具：\n"
        f"{tools_text}\n"
        "请严格只输出一个 JSON 对象，不要输出 Markdown 代码块或其它文字：\n"
        '- 需要调用工具时：{"tool":"工具名","arguments":{...}}\n'
        '- 可以回答时：{"answer":"最终回答，使用 Markdown，并用 [[news:ID]] 标注来源"}\n'
        "每轮只能调用一个工具；拿到结果后继续判断，直到可以回答。"
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _json_tool_call(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = obj.get("function") if isinstance(obj.get("function"), dict) else {}
    name = obj.get("tool") or obj.get("name") or function.get("name") or ""
    arguments = obj.get("arguments") or obj.get("args") or function.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            arguments = {}
    return str(name), arguments if isinstance(arguments, dict) else {}


def finalize_citations(answer: str, known_refs: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """只保留本轮工具确实返回过的引用，并把 [[news:12]] 重写为 [1]。"""
    order: list[str] = []
    seen: set[str] = set()

    def replace(match: re.Match) -> str:
        key = f"{match.group(1)}:{match.group(2)}"
        if key not in known_refs:
            return ""
        if key not in seen:
            seen.add(key)
            order.append(key)
        return f"[{order.index(key) + 1}]"

    content = CITATION_RE.sub(replace, answer or "")
    citations = []
    for index, key in enumerate(order, start=1):
        meta = dict(known_refs[key])
        meta["n"] = index
        citations.append(meta)
    return content, citations


def _assistant_tool_message(response: llm_client.LLMResponse, calls: list[llm_client.ToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in calls
        ],
    }


async def _safe_emit(emit: EmitFn | None, event: str, data: dict[str, Any]) -> None:
    if not emit:
        return
    try:
        await emit(event, data)
    except Exception:
        logger.debug("SSE 发送失败（客户端可能已断开）：%s", event)


class _CitationStreamer:
    """流式输出时增量重写引用：[[news:12]] -> [1]。

    对可能被切分的未闭合标记做保守缓冲，避免把 `[[news:` 直接吐给前端。
    """

    def __init__(self, known_refs: dict[str, dict[str, Any]]):
        self.refs = known_refs
        self.buf = ""
        self.order: list[str] = []
        self.seen: set[str] = set()

    def _replace(self, text: str) -> str:
        def repl(match: re.Match) -> str:
            key = f"{match.group(1)}:{match.group(2)}"
            if key not in self.refs:
                return ""
            if key not in self.seen:
                self.seen.add(key)
                self.order.append(key)
            return f"[{self.order.index(key) + 1}]"

        return STREAM_CITATION_RE.sub(repl, text)

    def feed(self, text: str) -> str:
        self.buf += text
        cut = len(self.buf)
        open2 = self.buf.rfind("[[")
        if open2 != -1 and "]]" not in self.buf[open2:]:
            # 标记不完整（可能被切分），从 `[[` 起全部缓冲
            cut = open2
        elif self.buf.endswith("["):
            # 可能是 `[[` 的前半个字符
            cut = len(self.buf) - 1
        head, self.buf = self.buf[:cut], self.buf[cut:]
        return self._replace(head)

    def finish(self) -> str:
        tail, self.buf = self.buf, ""
        return self._replace(tail)


async def _invoke_model(
    endpoint: dict,
    messages: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    emit: EmitFn | None,
    known_refs: dict[str, dict[str, Any]],
    tools: list[dict] | None = None,
    stream: bool = True,
    purpose: str = "chat",
) -> tuple[llm_client.LLMResponse, int]:
    """调用模型并实时推送增量。返回 (响应, 已流式输出的字符数)。

    字符数为 0 表示没有流式输出（JSON 协议/回退），调用方需要整段输出答案。
    """
    streamer = _CitationStreamer(known_refs)
    emitted = 0
    any_output = False

    async def on_reasoning(text: str):
        nonlocal any_output
        any_output = True
        await _safe_emit(emit, "reasoning", {"text": text})

    async def on_delta(text: str):
        nonlocal emitted, any_output
        any_output = True
        chunk = streamer.feed(text)
        if chunk:
            emitted += len(chunk)
            await _safe_emit(emit, "token", {"text": chunk})

    kwargs: dict[str, Any] = {
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS_PER_TURN,
        "timeout": config["llm_timeout"],
        "purpose": purpose,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    if not stream:
        return await llm_client.chat_endpoint(endpoint, messages, **kwargs), 0

    try:
        response = await llm_client.chat_endpoint(
            endpoint,
            messages,
            stream=True,
            on_delta=on_delta,
            on_reasoning=on_reasoning,
            **kwargs,
        )
    except llm_client.ToolsUnsupported:
        raise
    except llm_client.LLMError as exc:
        # 还没吐出任何内容时回退为整段返回；已输出则保持原样，避免重复
        if any_output:
            raise
        logger.info("流式调用失败，回退非流式：%s", exc)
        return await llm_client.chat_endpoint(endpoint, messages, **kwargs), 0

    tail = streamer.finish()
    if tail:
        emitted += len(tail)
        await _safe_emit(emit, "token", {"text": tail})
    return response, emitted


async def _pre_retrieve(db: AsyncSession, query: str) -> str:
    result = await retrieval.search_news(db, query, limit=6)
    items = result.get("items") or []
    return json.dumps(items, ensure_ascii=False)


async def run_turn(
    conversation_id: int | None,
    user_text: str,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    config = load_config()
    user_text = (user_text or "").strip()
    if not config["enabled"]:
        await _safe_emit(emit, "error", {"code": "chat_disabled", "message": "对话助手未启用"})
        await _safe_emit(emit, "done", {"content": "", "citations": [], "usage": {}})
        return {"error": "chat_disabled"}
    if not user_text:
        await _safe_emit(emit, "error", {"code": "empty_message", "message": "请输入内容"})
        await _safe_emit(emit, "done", {"content": "", "citations": [], "usage": {}})
        return {"error": "empty_message"}
    if daily_tokens_used() >= config["daily_budget"]:
        message = "今日 Token 预算已用完，请明天再试或调高 AGENT_DAILY_TOKEN_BUDGET"
        await _safe_emit(emit, "error", {"code": "budget_exceeded", "message": message})
        await _safe_emit(emit, "done", {"content": "", "citations": [], "usage": {}})
        return {"error": "budget_exceeded"}

    # 1. 会话 + 用户消息（短事务）
    async with get_session() as db:
        conversation = (
            await conversation_service.get_conversation(db, conversation_id)
            if conversation_id
            else None
        )
        if not conversation:
            conversation = await conversation_service.create_conversation(db, user_text[:30])
        user_message = await conversation_service.add_message(db, conversation.id, "user", user_text)
        conversation_summary = conversation.summary
        await db.commit()
        conv_id = conversation.id
        user_message_id = user_message.id

    await _safe_emit(
        emit,
        "meta",
        {"conversation_id": conv_id, "user_message_id": user_message_id},
    )
    await _safe_emit(emit, "status", {"stage": "thinking"})

    # 2. 历史 + 系统提示 + 模型端点（另开短事务，避免跨 LLM 调用持有）
    async with get_session() as db:
        history = await conversation_service.load_history(
            db, conv_id, config["history_messages"], before_id=user_message_id
        )
        system_prompt = await _build_system_prompt(db)
        endpoint = await get_active_endpoint(db, "llm")

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    trace: list[dict[str, Any]] = []
    known_refs: dict[str, dict[str, Any]] = {}
    answer = ""
    assistant_message_id: int | None = None

    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        answer = "还没有配置可用的大语言模型。请在「设置 → 模型与服务配置」中启用一个大语言模型后再试。"
        await _safe_emit(emit, "error", {"code": "llm_not_configured", "message": answer})
        async with get_session() as db:
            saved = await conversation_service.add_message(
                db, conv_id, "assistant", answer, status="error"
            )
            await db.commit()
            assistant_message_id = saved.id
        answer, citations = finalize_citations(answer, known_refs)
        await _safe_emit(emit, "token", {"text": answer})
        await _safe_emit(
            emit,
            "done",
            {
                "conversation_id": conv_id,
                "assistant_message_id": assistant_message_id,
                "content": answer,
                "citations": citations,
                "usage": usage_total,
            },
        )
        return {"conversation_id": conv_id, "content": answer, "citations": citations, "usage": usage_total}

    protocol = llm_client.protocol_for(endpoint)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if conversation_summary:
        messages.append({"role": "system", "content": f"更早对话的摘要：\n{conversation_summary}"})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    if protocol == "none":
        async with get_session() as db:
            context = await _pre_retrieve(db, user_text)
        messages.append(
            {
                "role": "system",
                "content": (
                    "<news_data>\n"
                    f"{context}\n"
                    "</news_data>\n"
                    "以上是从用户新闻库预检索到的资料（可能为空）。请基于这些资料回答；"
                    "不足时明确说明资料不足，不要编造。引用格式 [[news:ID]]。"
                ),
            }
        )
        response, answer_streamed = await _invoke_model(
            endpoint,
            messages,
            config=config,
            emit=emit,
            known_refs=known_refs,
            stream=True,
        )
        _add_usage(usage_total, response.usage)
        _record_usage(response.usage)
        answer = response.content
    else:
        tool_calls_used = 0
        consecutive_errors = 0
        force_answer = False
        answer_streamed = False

        for step in range(config["max_iterations"]):
            _fit_messages(messages, config["context_tokens"] - OUTPUT_RESERVE)

            if protocol == "native":
                try:
                    response, streamed = await _invoke_model(
                        endpoint,
                        messages,
                        config=config,
                        emit=emit,
                        known_refs=known_refs,
                        tools=tool_registry.tool_schemas(),
                        stream=True,
                    )
                except llm_client.ToolsUnsupported:
                    logger.info("端点不支持原生 tools，切换 JSON 协议")
                    protocol = "json"
                    messages[0]["content"] = str(messages[0].get("content") or "") + _json_instruction()
                    continue
            else:
                response, streamed = await _invoke_model(
                    endpoint,
                    messages,
                    config=config,
                    emit=emit,
                    known_refs=known_refs,
                    stream=False,
                )

            _add_usage(usage_total, response.usage)
            _record_usage(response.usage)

            if protocol == "native" and response.tool_calls:
                remaining = config["max_tool_calls"] - tool_calls_used
                calls = response.tool_calls[:remaining]
                if not calls:
                    force_answer = True
                    break

                # 模型可能先吐了半段文字再决定调工具，清掉这段草稿
                if streamed:
                    await _safe_emit(emit, "reset", {"reason": "tool_call"})

                await _safe_emit(emit, "status", {"stage": "tool"})
                messages.append(_assistant_tool_message(response, calls))

                # 先发 tool_call，再执行：委派类工具可能跑几分钟，用户需要立即看到进度
                for call in calls:
                    await _safe_emit(
                        emit,
                        "tool_call",
                        {"id": call.id, "name": call.name, "arguments": call.arguments},
                    )

                async def run_tool(call):
                    async def progress(text: str):
                        await _safe_emit(emit, "status", {"stage": "tool", "text": text})

                    return await tool_registry.execute_tool(
                        call.name, call.arguments, emit=progress
                    )

                # 委派任务（采集/简报）耗时很长，串行执行；其它只读工具保持并发
                if any(tool_registry.is_delegate(call.name) for call in calls):
                    results = []
                    for call in calls:
                        try:
                            results.append(await run_tool(call))
                        except Exception as exc:  # 保持与 gather(return_exceptions) 一致
                            results.append(exc)
                else:
                    results = await asyncio.gather(
                        *[run_tool(call) for call in calls],
                        return_exceptions=True,
                    )

                for call, result in zip(calls, results):
                    if isinstance(result, Exception):
                        error_result = tool_registry.ToolResult(
                            ok=False, error=f"{type(result).__name__}: {result}"
                        )
                        result = error_result
                    tool_calls_used += 1
                    await _safe_emit(
                        emit,
                        "tool_result",
                        {
                            "id": call.id,
                            "name": call.name,
                            "ok": result.ok,
                            "summary": result.summary or result.error,
                            "count": result.count,
                        },
                    )
                    trace.append(
                        {
                            "name": call.name,
                            "arguments": call.arguments,
                            "ok": result.ok,
                            "summary": result.summary or result.error,
                            "count": result.count,
                        }
                    )
                    if result.ok:
                        known_refs.update(result.refs)
                        consecutive_errors = 0
                    else:
                        consecutive_errors += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": result.to_payload(),
                        }
                    )

                if consecutive_errors >= 3:
                    force_answer = True
                    break
                continue

            if protocol == "json":
                obj = _parse_json_object(response.content)
                if obj is None:
                    # 模型直接给了自然语言答案（JSON 协议不走流式）
                    answer = response.content
                    answer_streamed = False
                    break
                name, arguments = _json_tool_call(obj)
                if name:
                    if tool_calls_used >= config["max_tool_calls"]:
                        force_answer = True
                        break
                    await _safe_emit(emit, "status", {"stage": "tool"})
                    call_id = f"json_{tool_calls_used + 1}"
                    await _safe_emit(
                        emit, "tool_call", {"id": call_id, "name": name, "arguments": arguments}
                    )

                    async def progress(text: str):
                        await _safe_emit(emit, "status", {"stage": "tool", "text": text})

                    result = await tool_registry.execute_tool(name, arguments, emit=progress)
                    tool_calls_used += 1
                    await _safe_emit(
                        emit,
                        "tool_result",
                        {
                            "id": call_id,
                            "name": name,
                            "ok": result.ok,
                            "summary": result.summary or result.error,
                            "count": result.count,
                        },
                    )
                    trace.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "ok": result.ok,
                            "summary": result.summary or result.error,
                            "count": result.count,
                        }
                    )
                    if result.ok:
                        known_refs.update(result.refs)
                        consecutive_errors = 0
                    else:
                        consecutive_errors += 1
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[TOOL_RESULT name={name}]\n{result.to_payload()}",
                        }
                    )
                    if consecutive_errors >= 3:
                        force_answer = True
                        break
                    continue
                answer = str(obj.get("answer") or obj.get("content") or "")
                answer_streamed = False
                break

            answer = response.content
            answer_streamed = bool(streamed)
            break

        if not answer and not force_answer:
            answer = ""
        if force_answer or not answer.strip():
            await _safe_emit(emit, "status", {"stage": "answering"})
            messages.append(
                {
                    "role": "user",
                    "content": "已达到工具调用上限。请基于已有信息直接回答；不确定的地方明确说明。",
                }
            )
            _fit_messages(messages, config["context_tokens"] - OUTPUT_RESERVE)
            response, answer_streamed = await _invoke_model(
                endpoint,
                messages,
                config=config,
                emit=emit,
                known_refs=known_refs,
                stream=True,
            )
            _add_usage(usage_total, response.usage)
            _record_usage(response.usage)
            answer = response.content

    answer, citations = finalize_citations(answer or "", known_refs)
    if not answer.strip():
        answer = "抱歉，我暂时没能生成回答，请稍后再试。"
        answer_streamed = False
    await _safe_emit(emit, "status", {"stage": "answering"})

    async with get_session() as db:
        saved = await conversation_service.add_message(
            db,
            conv_id,
            "assistant",
            answer,
            tool_calls=json.dumps(trace, ensure_ascii=False) if trace else None,
            citations=json.dumps(citations, ensure_ascii=False) if citations else None,
            usage=json.dumps(usage_total, ensure_ascii=False),
        )
        await db.commit()
        assistant_message_id = saved.id

    if not answer_streamed:
        await _safe_emit(emit, "token", {"text": answer})
    for citation in citations:
        await _safe_emit(emit, "citation", citation)
    await _safe_emit(emit, "usage", usage_total)
    await _safe_emit(
        emit,
        "done",
        {
            "conversation_id": conv_id,
            "assistant_message_id": assistant_message_id,
            "content": answer,
            "citations": citations,
            "usage": usage_total,
            "tool_trace": trace,
        },
    )

    return {
        "conversation_id": conv_id,
        "assistant_message_id": assistant_message_id,
        "content": answer,
        "citations": citations,
        "usage": usage_total,
        "tool_trace": trace,
    }
