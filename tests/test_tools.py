import pytest

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
    result = await tools.execute_tool("list_categories", {"unexpected": "value"})
    assert result.ok is True
    assert result.count >= 0


def test_schemas_have_openai_shape():
    for schema in tools.tool_schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert function["description"]
        assert function["parameters"]["type"] == "object"
