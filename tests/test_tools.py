import pytest

from app.services import memory_service
from app.services.agent import tools


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    result = await tools.execute_tool("not_a_tool", {})
    assert result.ok is False
    assert "未知工具" in result.error


@pytest.mark.asyncio
async def test_missing_required_argument_returns_error():
    result = await tools.execute_tool("search_news", {})
    assert result.ok is False
    assert "缺少必填参数" in result.error


@pytest.mark.asyncio
async def test_unknown_arguments_are_ignored():
    result = await tools.execute_tool("list_tags", {"unexpected": "value"})
    assert result.ok is True
    assert result.count >= 0


def test_schemas_have_openai_shape():
    for schema in tools.tool_schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert function["description"]
        assert function["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_memory_tools_write_and_forget(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_service, "memory_dir", lambda: tmp_path)
    monkeypatch.setenv("AGENT_MEMORY_ENABLED", "true")

    result = await tools.execute_tool(
        "remember",
        {"content": "跟踪 AI 芯片（科技）", "section": "watch"},
    )
    assert result.ok is True
    text = await memory_service.read_memory()
    assert "AI 芯片" in memory_service.read_section(text, "watch")

    read = await tools.execute_tool("read_memory", {})
    assert read.ok is True
    assert "AI 芯片" in read.data["sections"]["watch"]

    removed = await tools.execute_tool("forget_memory", {"query": "AI 芯片"})
    assert removed.ok is True
    assert removed.data["removed"] == 1


def test_memory_tools_are_in_schemas():
    names = {schema["function"]["name"] for schema in tools.tool_schemas()}
    assert {"read_memory", "remember", "forget_memory", "get_user_profile",
            "get_recommendations", "submit_feedback"} <= names
