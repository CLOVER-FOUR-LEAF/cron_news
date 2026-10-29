import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.database import get_session, init_db
from app.models import Message, News
from app.services import llm_client, memory_service
from app.services.agent import runtime


async def _seed_news() -> int:
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        news = News(
            title=f"英伟达发布新一代 AI 芯片 {token}",
            summary="面向推理场景，能效提升明显。",
            content="英伟达发布新一代 AI 芯片。",
            source="测试来源",
            source_url=f"https://example.com/{token}",
            collected_at=datetime.now(),
            tags='["AI芯片", "英伟达"]',
        )
        db.add(news)
        await db.commit()
        return news.id


def _endpoint() -> dict:
    return {
        "id": 999,
        "base_url": "http://fake/v1",
        "api_key": "fake-key",
        "model_id": "fake-chat",
        "provider": "fake",
        "provider_name": "fake",
        "name": "fake",
    }


@pytest.mark.asyncio
async def test_native_tool_loop_and_citation(monkeypatch):
    news_id = await _seed_news()

    async def fake_get_active_endpoint(db, config_type):
        return _endpoint() if config_type == "llm" else None

    async def fake_chat_endpoint(endpoint, messages, **kwargs):
        saw_tool = any(message.get("role") == "tool" for message in messages)
        if kwargs.get("tools") and not saw_tool:
            return llm_client.LLMResponse(
                tool_calls=[
                    llm_client.ToolCall(id="call_1", name="search_news", arguments={"query": "英伟达"})
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
        return llm_client.LLMResponse(
            content=f"英伟达发布了新芯片。[[news:{news_id}]]",
            usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        )

    monkeypatch.setattr(runtime, "get_active_endpoint", fake_get_active_endpoint)
    monkeypatch.setattr(llm_client, "chat_endpoint", fake_chat_endpoint)
    monkeypatch.setattr(llm_client, "protocol_for", lambda endpoint: "native")

    events = []

    async def emit(event, data):
        events.append((event, data))

    result = await runtime.run_turn(None, "英伟达最近有什么新闻？", emit)

    assert result["content"].endswith("[1]")
    assert result["citations"][0]["news_id"] == news_id
    assert result["tool_trace"][0]["name"] == "search_news"
    assert any(event == "tool_call" for event, _ in events)
    assert events[-1][0] == "done"

    async with get_session() as db:
        messages = (
            await db.execute(
                select(Message).where(Message.conversation_id == result["conversation_id"]).order_by(Message.id)
            )
        ).scalars().all()
    assert [message.role for message in messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_json_protocol_fallback(monkeypatch):
    news_id = await _seed_news()

    async def fake_get_active_endpoint(db, config_type):
        return _endpoint() if config_type == "llm" else None

    async def fake_chat_endpoint(endpoint, messages, **kwargs):
        saw_tool_result = any("[TOOL_RESULT" in str(message.get("content") or "") for message in messages)
        if not saw_tool_result:
            return llm_client.LLMResponse(
                content=json.dumps({"tool": "search_news", "arguments": {"query": "英伟达"}}, ensure_ascii=False),
                usage={"total_tokens": 10},
            )
        return llm_client.LLMResponse(
            content=json.dumps({"answer": f"英伟达有新消息。[[news:{news_id}]]"}, ensure_ascii=False),
            usage={"total_tokens": 10},
        )

    monkeypatch.setattr(runtime, "get_active_endpoint", fake_get_active_endpoint)
    monkeypatch.setattr(llm_client, "chat_endpoint", fake_chat_endpoint)
    monkeypatch.setattr(llm_client, "protocol_for", lambda endpoint: "json")

    result = await runtime.run_turn(None, "英伟达有什么？", emit=None)
    assert result["content"].endswith("[1]")
    assert result["citations"][0]["news_id"] == news_id


@pytest.mark.asyncio
async def test_chat_can_write_long_term_memory(monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(memory_service, "memory_dir", lambda: tmp_path)
    monkeypatch.setenv("AGENT_MEMORY_ENABLED", "true")

    async def fake_get_active_endpoint(db, config_type):
        return _endpoint() if config_type == "llm" else None

    async def fake_chat_endpoint(endpoint, messages, **kwargs):
        saw_tool = any(message.get("role") == "tool" for message in messages)
        if kwargs.get("tools") and not saw_tool:
            return llm_client.LLMResponse(
                tool_calls=[
                    llm_client.ToolCall(
                        id="call_mem",
                        name="remember",
                        arguments={"content": "跟踪 AI 芯片（科技）", "section": "watch"},
                    )
                ],
                usage={"total_tokens": 10},
            )
        return llm_client.LLMResponse(content="记住了：你正在跟踪 AI 芯片。", usage={"total_tokens": 8})

    monkeypatch.setattr(runtime, "get_active_endpoint", fake_get_active_endpoint)
    monkeypatch.setattr(llm_client, "chat_endpoint", fake_chat_endpoint)
    monkeypatch.setattr(llm_client, "protocol_for", lambda endpoint: "native")

    result = await runtime.run_turn(None, "记住：我最近在跟踪 AI 芯片", emit=None)
    assert "记住" in result["content"]

    text = await memory_service.read_memory()
    watch = memory_service.read_section(text, "watch")
    assert "AI 芯片" in watch
    assert any(item["name"] == "remember" for item in result["tool_trace"])
