"""大模型内置联网：识别、能力判定与响应解析。"""

from datetime import datetime

import pytest

from app.services import native_search


def test_resolve_spec_by_provider_and_host():
    assert native_search.resolve_spec("DeepSeek").kind == "deepseek"
    assert native_search.resolve_spec("", "https://api.deepseek.com/v1").kind == "deepseek"
    assert native_search.resolve_spec("Kimi（月之暗面）").kind == "kimi"
    assert native_search.resolve_spec("", "https://open.bigmodel.cn/api/paas/v4").kind == "zhipu"
    assert native_search.resolve_spec("MiMo（小米）").kind == "mimo"
    # 聚合网关上的同款模型不应被误判为厂商自带搜索
    assert native_search.resolve_spec("opencodego", "https://opencode.ai/inference/go/openai/v1") is None


def test_capability_available_rules():
    # 从未探测过的独立搜索接口：乐观放行
    assert native_search.capability_available("deepseek", "", "", False, None) is True
    # 对话工具型必须探测通过
    assert native_search.capability_available("mimo", "", "", False, None) is False
    assert native_search.capability_available("mimo", "", "", True, None) is False
    # 已经探测过的以结果为准
    assert native_search.capability_available("deepseek", "", "", True, datetime.now()) is True
    assert native_search.capability_available("deepseek", "", "", False, datetime.now()) is False


def test_parse_deepseek():
    hits = native_search.parse_deepseek(
        {
            "results": [
                {
                    "title": "AI 新闻",
                    "url": "https://example.com/a",
                    "snippet": "摘要",
                    "summary": "更长摘要",
                    "site_name": "示例",
                    "date": "2026-10-28T08:00:00Z",
                }
            ]
        }
    )
    assert len(hits) == 1
    assert hits[0].title == "AI 新闻"
    assert hits[0].snippet == "摘要"
    assert hits[0].source == "示例"


def test_parse_kimi_with_chunks():
    hits = native_search.parse_kimi(
        {
            "search_results": [
                {
                    "title": "Kimi 结果",
                    "url": "https://example.com/b",
                    "site_name": "站点",
                    "date": "2026-10-28",
                    "chunks": [{"text": "第一段"}, {"text": "第二段"}],
                }
            ]
        }
    )
    assert hits[0].snippet == "第一段 第二段"


def test_parse_zhipu_fields():
    hits = native_search.parse_zhipu(
        {
            "search_result": [
                {
                    "title": "智谱结果",
                    "link": "https://example.com/c",
                    "content": "内容摘要",
                    "media": "媒体名",
                    "publish_date": "2026-10-27",
                }
            ]
        }
    )
    assert hits[0].url == "https://example.com/c"
    assert hits[0].source == "媒体名"
    assert hits[0].published_date == "2026-10-27"


def test_parse_mimo_annotations_and_dedupe():
    hits = native_search.parse_mimo(
        {
            "choices": [
                {
                    "message": {
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/d",
                                "title": "MiMo 来源",
                                "summary": "摘要",
                                "site_name": "站点",
                                "publish_time": "2026-10-28",
                            },
                            {
                                "type": "url_citation",
                                "url": "https://example.com/d",
                                "title": "重复",
                            },
                        ]
                    }
                }
            ]
        }
    )
    assert len(hits) == 1
    assert hits[0].title == "MiMo 来源"


def _hit(url: str, published: str = "") -> native_search.SearchHit:
    return native_search.SearchHit(title=url, url=url, published_date=published)


def test_hour_filter_keeps_undated_when_dates_are_not_clear_majority():
    old = "2020-01-01"
    hits = [_hit(f"https://example.com/old{i}", old) for i in range(5)]
    hits += [_hit(f"https://example.com/undated{i}") for i in range(5)]
    assert len(native_search._filter_by_hours(hits, 24)) == 10


def test_hour_filter_drops_old_dated_but_keeps_undated():
    old = "2020-01-01"
    hits = [_hit(f"https://example.com/old{i}", old) for i in range(6)]
    hits += [_hit(f"https://example.com/undated{i}") for i in range(4)]
    result = native_search._filter_by_hours(hits, 24)
    assert len(result) == 4
    assert all("undated" in hit.url for hit in result)


@pytest.mark.asyncio
async def test_run_native_search_dispatch_and_body(monkeypatch):
    captured = {}

    async def fake_post(url, headers, body, timeout):
        captured["url"] = url
        captured["body"] = body
        return {
            "results": [
                {"title": "T", "url": "https://example.com/e", "snippet": "S", "site_name": "X"}
            ]
        }

    monkeypatch.setattr(native_search, "_post_json", fake_post)
    hits = await native_search.run_native_search(
        "deepseek",
        "https://api.deepseek.com/v1",
        "key",
        "deepseek-flash",
        "AI 芯片",
        max_results=5,
        hours=24,
    )
    assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
    assert captured["body"]["count"] == 5
    assert "after:" in captured["body"]["query"]
    assert hits[0]["url"] == "https://example.com/e"


@pytest.mark.asyncio
async def test_detect_mimo_without_annotations_is_unavailable(monkeypatch):
    async def fake_post(url, headers, body, timeout):
        return {"choices": [{"message": {"content": "没有引用"}}]}

    monkeypatch.setattr(native_search, "_post_json", fake_post)
    result = await native_search.detect_native_search(
        "mimo", "https://api.xiaomimimo.com/v1", "key", "mimo-v2.6-pro"
    )
    assert result["available"] is False
    assert "联网插件" in result["detail"]


@pytest.mark.asyncio
async def test_detect_standalone_zero_hits_is_unavailable(monkeypatch):
    async def fake_run(*args, **kwargs):
        return []

    monkeypatch.setattr(native_search, "run_native_search", fake_run)
    result = await native_search.detect_native_search(
        "deepseek", "https://api.deepseek.com/v1", "key", "deepseek-flash"
    )
    assert result["available"] is False
    assert "没有返回结果" in result["detail"]
