"""自然语言偏好：兜底解析、模型解析与 .env 写回。"""

import json

import pytest

from app.services import preferences


@pytest.fixture
def env_store(monkeypatch):
    store: dict[str, str] = {}

    monkeypatch.setattr(preferences, "read_env_file", lambda: dict(store))

    def fake_write(env):
        store.clear()
        store.update(env)

    monkeypatch.setattr(preferences, "write_env_file", fake_write)
    return store


@pytest.mark.asyncio
async def test_fallback_analysis_extracts_tags_hours_and_items(env_store, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("no llm")

    monkeypatch.setattr(preferences.llm_client, "chat_text", fail)

    result = await preferences.analyze_and_save(
        None,
        "我关注 AI 芯片和美股，不想看娱乐八卦",
        "一般早上 8 点和晚上 9 点看，一次 10 条",
        "smart",
    )
    tags = result["interests"]["tags"]
    assert "人工智能" in tags
    assert "芯片" in tags
    assert "娱乐" not in tags
    assert result["schedule"]["preferred_hours"] == [8, 21]
    assert result["schedule"]["items_per_run"] == 10
    assert result["analyzed_by"] == "fallback"

    assert json.loads(env_store["AGENT_INTERESTS_TAGS"]) == tags
    assert env_store["TASK_INTERVAL_HOURS"] == "8"
    assert env_store["TASK_START_HOUR"] == "8"
    assert env_store["AGENT_MAX_ITEMS_PER_RUN"] == "10"
    assert env_store["TASK_SELECTED_TAGS"] == "[]"
    assert preferences.schedule_plan()["preferred_hours"] == [8, 21]
    assert preferences.interests_tags() == tags


@pytest.mark.asyncio
async def test_llm_analysis_is_sanitized(env_store, monkeypatch):
    async def fake_chat(*args, **kwargs):
        return json.dumps(
            {
                "interests": {"tags": ["AI大模型", "芯片", "芯片", "新闻"], "summary": "关注 AI 与芯片"},
                "schedule": {
                    "preferred_hours": [21, 8, 25],
                    "interval_hours": 99,
                    "start_hour": -1,
                    "lead_minutes": 10,
                    "items_per_run": 500,
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(preferences.llm_client, "chat_text", fake_chat)

    result = await preferences.analyze_and_save(
        None, "AI 大模型和芯片", "晚上 9 点、早上 8 点看", "smart"
    )
    assert result["analyzed_by"] == "llm"
    assert result["interests"]["tags"] == ["AI大模型", "芯片"]
    assert result["schedule"]["preferred_hours"] == [8, 21]
    assert result["schedule"]["interval_hours"] == 24
    assert result["schedule"]["start_hour"] == 0
    assert result["schedule"]["lead_minutes"] == 15
    assert result["schedule"]["items_per_run"] == 80
    assert result["schedule"]["queries_per_tag"] == 3


@pytest.mark.asyncio
async def test_fixed_mode_does_not_override_manual_schedule(env_store, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("no llm")

    monkeypatch.setattr(preferences.llm_client, "chat_text", fail)
    await preferences.analyze_and_save(None, "关注芯片", "早上 8 点看", "fixed")
    assert "TASK_INTERVAL_HOURS" not in env_store
    assert "TASK_START_HOUR" not in env_store
    assert env_store["AGENT_MAX_ITEMS_PER_RUN"] == "12"
