"""搜索厂商适配层：新预设的解析与响应结构。"""

import httpx
import pytest

from app.services import search_providers as sp


@pytest.mark.asyncio
async def test_brave_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "k"
        assert request.url.params["freshness"] == "pd"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave 结果",
                            "url": "https://example.com/a",
                            "description": "摘要",
                            "age": "1 day ago",
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await sp.BraveProvider().search(
            client, "https://api.search.brave.com", "k", "AI", 5, 24
        )
    assert hits[0].title == "Brave 结果"
    assert hits[0].published_date == "1 day ago"


@pytest.mark.asyncio
async def test_serper_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "k"
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Serper 结果",
                        "link": "https://example.com/b",
                        "snippet": "摘要",
                        "date": "Oct 28, 2026",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await sp.SerperProvider().search(
            client, "https://google.serper.dev", "k", "AI", 5, 24
        )
    assert hits[0].url == "https://example.com/b"
    assert hits[0].source == "example.com"


@pytest.mark.asyncio
async def test_serpapi_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "k"
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "SerpAPI 结果",
                        "link": "https://example.com/c",
                        "snippet": "摘要",
                        "source": "示例",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await sp.SerpApiProvider().search(
            client, "https://serpapi.com", "k", "AI", 5, None
        )
    assert hits[0].source == "示例"


@pytest.mark.asyncio
async def test_firecrawl_v2_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/search"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Firecrawl 结果",
                            "url": "https://example.com/d",
                            "description": "摘要",
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await sp.FirecrawlProvider().search(
            client, "https://api.firecrawl.dev", "k", "AI", 5, None
        )
    assert hits[0].title == "Firecrawl 结果"


@pytest.mark.asyncio
async def test_jina_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "Jina 结果",
                        "url": "https://example.com/e",
                        "content": "正文摘要",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await sp.JinaSearchProvider().search(
            client, "https://s.jina.ai", "k", "AI", 5, None
        )
    assert hits[0].snippet == "正文摘要"


def test_provider_resolution_and_key_requirement():
    assert sp.resolve_provider("Brave Search", "").key == "brave"
    assert sp.resolve_provider("", "https://google.serper.dev").key == "serper"
    assert sp.resolve_provider("", "https://serpapi.com").key == "serpapi"
    assert sp.resolve_provider("", "https://s.jina.ai").key == "jina"
    assert sp.resolve_provider("", "https://api.firecrawl.dev").key == "firecrawl"
    assert sp.provider_requires_key("searxng", "") is False
    assert sp.provider_requires_key("tavily", "") is True
