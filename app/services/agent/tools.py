"""Agent 工具注册表。

Phase 1 全部为只读工具。工具结果统一为 {ok, summary, count, data}，异常一律转成
{ok:false, error} 回传给模型，绝不让异常冒泡中断对话。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import category as category_crud
from app.crud import news as news_crud
from app.database import get_session
from app.env_store import read_env_file
from app.services import brief_service, retrieval


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    summary: str = ""
    count: int = 0
    error: str | None = None
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_payload(self, max_chars: int = 8000) -> str:
        if not self.ok:
            payload = json.dumps({"ok": False, "error": self.error or "工具执行失败"}, ensure_ascii=False)
        else:
            payload = json.dumps(
                {"ok": True, "summary": self.summary, "count": self.count, "data": self.data},
                ensure_ascii=False,
            )
        if len(payload) > max_chars:
            payload = json.dumps(
                {"ok": self.ok, "summary": self.summary, "count": self.count, "data": "结果过长，已截断"},
                ensure_ascii=False,
            )
        return payload


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]
    read_only: bool = True
    timeout: float = 20.0


def _error(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message)


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


async def _search_news(
    db: AsyncSession,
    query: str,
    category: str | None = None,
    days: int | None = None,
    limit: int = 8,
) -> ToolResult:
    limit = _as_int(limit, 8, 1, 12)
    day_value = _as_int(days, 1, 1, 365) if days is not None else None
    result = await retrieval.search_news(db, query, category=category, days=day_value, limit=limit)
    items = result.get("items") or []
    mode = result.get("mode")
    labels = {"hybrid": "混合检索", "lexical": "关键词检索", "semantic": "语义检索"}
    label = labels.get(mode, "检索")
    summary = f"{label}找到 {len(items)} 条相关新闻" if items else f"{label}未找到相关新闻"
    return ToolResult(
        ok=True,
        data={"mode": mode, "terms": result.get("terms"), "items": items},
        summary=summary,
        count=len(items),
        refs=retrieval.refs_from_items(items),
    )


async def _get_news(db: AsyncSession, news_id: int, max_chars: int = 3000) -> ToolResult:
    news_id = _as_int(news_id, 0, 0, 2_000_000_000)
    max_chars = _as_int(max_chars, 3000, 500, 6000)
    news = await news_crud.get_news_by_id(db, news_id)
    if not news:
        return _error(f"新闻 {news_id} 不存在")
    content = (news.content or news.summary or "")[:max_chars]
    item = retrieval.to_item(news, "detail")
    item["content"] = content
    item["summary"] = (news.summary or "")[:500]
    return ToolResult(
        ok=True,
        data=item,
        summary=f"读取新闻《{news.title[:40]}》",
        count=1,
        refs=retrieval.refs_from_items([item]),
    )


async def _list_news(
    db: AsyncSession,
    category: str | None = None,
    days: int = 1,
    unread_only: bool = False,
    limit: int = 10,
) -> ToolResult:
    days = _as_int(days, 1, 1, 90)
    limit = _as_int(limit, 10, 1, 20)
    unread_only = _as_bool(unread_only)
    result = await retrieval.list_recent(
        db, category=category, days=days, unread_only=unread_only, limit=limit
    )
    items = result.get("items") or []
    summary = f"最近 {days} 天找到 {len(items)} 条新闻" if items else "该时间范围内没有新闻"
    return ToolResult(
        ok=True,
        data={"days": days, "items": items},
        summary=summary,
        count=len(items),
        refs=retrieval.refs_from_items(items),
    )


async def _get_daily_brief(db: AsyncSession, category: str | None = None, date: str | None = None) -> ToolResult:
    if category:
        brief = (
            await brief_service.get_brief_by_date(db, category, date)
            if date
            else await brief_service.get_latest_brief(db, category)
        )
        if not brief:
            return ToolResult(ok=True, data=None, summary=f"「{category}」暂无简报", count=0)
        return ToolResult(
            ok=True,
            data={
                "category": brief.category_name,
                "date": brief.brief_date,
                "content": (brief.content or "")[:2500],
                "source": brief.source,
            },
            summary=f"读取「{category}」{brief.brief_date} 简报",
            count=1,
        )

    categories, _ = await category_crud.get_category_list(db)
    items = []
    consumed = 0
    for cat in categories:
        brief = await brief_service.get_latest_brief(db, cat.name)
        if not brief:
            continue
        content = (brief.content or "")[:1200]
        if consumed + len(content) > 6000:
            break
        consumed += len(content)
        items.append(
            {"category": brief.category_name, "date": brief.brief_date, "content": content}
        )
    return ToolResult(
        ok=True,
        data={"items": items},
        summary=f"读取 {len(items)} 个分类的最新简报",
        count=len(items),
    )


async def _get_related_news(db: AsyncSession, news_id: int, limit: int = 6) -> ToolResult:
    news_id = _as_int(news_id, 0, 0, 2_000_000_000)
    limit = _as_int(limit, 6, 1, 12)
    enabled = read_env_file().get("AGENT_RECOMMEND_ENABLED", "") == "true"
    news_items = await news_crud.get_related_news(db, news_id, limit, enabled)
    items = [retrieval.to_item(news, "related") for news in news_items]
    return ToolResult(
        ok=True,
        data={"items": items},
        summary=f"找到 {len(items)} 条相关新闻",
        count=len(items),
        refs=retrieval.refs_from_items(items),
    )


async def _get_news_stats(db: AsyncSession) -> ToolResult:
    stats = await news_crud.get_news_stats(db)
    data = {
        "total": stats.get("total"),
        "read_count": stats.get("read_count"),
        "unread_count": stats.get("unread_count"),
        "today_count": stats.get("today_count"),
        "category_stats": stats.get("category_stats"),
        "recent_reads": [
            {"id": item["id"], "title": item["title"], "category": item.get("category_name")}
            for item in (stats.get("recent_reads") or [])[:5]
        ],
    }
    return ToolResult(
        ok=True,
        data=data,
        summary=f"新闻库共 {data['total']} 条，未读 {data['unread_count']} 条，今日新增 {data['today_count']} 条",
        count=1,
    )


async def _list_categories(db: AsyncSession) -> ToolResult:
    categories, _ = await category_crud.get_category_list(db)
    items = [
        {"name": category.name, "color": category.color, "is_default": bool(category.is_default)}
        for category in categories
    ]
    return ToolResult(
        ok=True,
        data={"items": items},
        summary=f"共 {len(items)} 个分类",
        count=len(items),
    )


async def _get_reading_list(db: AsyncSession, kind: str = "favorites", limit: int = 10) -> ToolResult:
    if kind not in {"favorites", "later"}:
        kind = "favorites"
    limit = _as_int(limit, 10, 1, 30)
    news_items = await news_crud.get_my_list(db, kind)
    items = [retrieval.to_item(news, kind) for news in news_items[:limit]]
    label = "收藏" if kind == "favorites" else "稍后再读"
    return ToolResult(
        ok=True,
        data={"kind": kind, "items": items},
        summary=f"找到 {len(items)} 条{label}新闻",
        count=len(items),
        refs=retrieval.refs_from_items(items),
    )


async def _search_web(db: AsyncSession, query: str, hours: int = 24, max_results: int = 8) -> ToolResult:
    from app.services.ai_service import search_news as web_search

    hours = _as_int(hours, 24, 1, 720)
    max_results = _as_int(max_results, 8, 1, 10)
    hits = await web_search(db, query, max_results=max_results, hours=hours)
    items = []
    refs = {}
    for hit in hits:
        url = hit.get("url") or hit.get("link") or ""
        if not url:
            continue
        ref = f"web:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}"
        item = {
            "ref": ref,
            "title": hit.get("title") or "",
            "snippet": (hit.get("snippet") or hit.get("description") or "")[:240],
            "source": hit.get("source") or hit.get("domain") or "",
            "url": url,
            "published_date": hit.get("published_date") or hit.get("date") or "",
        }
        items.append(item)
        refs[ref] = {
            "ref": ref,
            "kind": "web",
            "title": item["title"],
            "source": item["source"],
            "source_url": url,
        }
    return ToolResult(
        ok=True,
        data={"items": items},
        summary=f"网络搜索返回 {len(items)} 条结果",
        count=len(items),
        refs=refs,
    )


TOOLS: dict[str, ToolSpec] = {
    "search_news": ToolSpec(
        name="search_news",
        description=(
            "在用户本地新闻库中检索。适合回答近期事件、主题、人物、公司相关问题。"
            "query 请传 2-6 个关键词（空格分隔），不要传完整句子。只能引用返回结果中的 ID。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "2-6 个关键词，空格分隔"},
                "category": {"type": "string", "description": "可选分类名，如 科技、政治"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
            },
            "required": ["query"],
        },
        handler=_search_news,
    ),
    "get_news": ToolSpec(
        name="get_news",
        description="读取单条新闻的完整标题、摘要、正文（按 max_chars 截断）、来源与原文链接。",
        parameters={
            "type": "object",
            "properties": {
                "news_id": {"type": "integer"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 6000, "default": 3000},
            },
            "required": ["news_id"],
        },
        handler=_get_news,
    ),
    "list_news": ToolSpec(
        name="list_news",
        description="按分类、时间、已读状态浏览新闻，按收录时间倒序。适合「今天有什么」「最近科技新闻」这类问题。",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 1},
                "unread_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
        },
        handler=_list_news,
    ),
    "get_daily_brief": ToolSpec(
        name="get_daily_brief",
        description="读取某分类某天的每日简报（Markdown）。不传日期取最新一期；不传分类返回各分类最新简报。",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
        handler=_get_daily_brief,
    ),
    "get_related_news": ToolSpec(
        name="get_related_news",
        description="读取与某条新闻相关的其它新闻（平台已计算的跨分类关联；未计算时回退同分类最新）。",
        parameters={
            "type": "object",
            "properties": {
                "news_id": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
            },
            "required": ["news_id"],
        },
        handler=_get_related_news,
    ),
    "get_news_stats": ToolSpec(
        name="get_news_stats",
        description="获取新闻库统计：总数、已读/未读、今日新增、分类分布、最近阅读。用于趋势和概览类问题。",
        parameters={"type": "object", "properties": {}},
        handler=_get_news_stats,
    ),
    "list_categories": ToolSpec(
        name="list_categories",
        description="列出所有新闻分类及颜色。",
        parameters={"type": "object", "properties": {}},
        handler=_list_categories,
    ),
    "get_reading_list": ToolSpec(
        name="get_reading_list",
        description="读取用户的收藏或稍后再读列表。",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["favorites", "later"], "default": "favorites"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
        },
        handler=_get_reading_list,
    ),
    "search_web": ToolSpec(
        name="search_web",
        description=(
            "调用联网搜索服务查找本地新闻库之外的公开信息。仅在本地库资料不足时使用，"
            "回答时必须区分「你的新闻库」与「网络搜索」。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 24},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 8},
            },
            "required": ["query"],
        },
        handler=_search_web,
    ),
}


def tool_schemas(enabled: set[str] | None = None) -> list[dict[str, Any]]:
    schemas = []
    for name, spec in TOOLS.items():
        if enabled is not None and name not in enabled:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
        )
    return schemas


def _prepare_args(spec: ToolSpec, arguments: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(arguments, dict):
        return {}, "工具参数必须是 JSON 对象"
    properties = spec.parameters.get("properties") or {}
    required = spec.parameters.get("required") or []
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None or key not in properties:
            continue
        cleaned[key] = value
    for key in required:
        if key not in cleaned:
            return cleaned, f"缺少必填参数：{key}"
    return cleaned, None


async def execute_tool(name: str, arguments: Any) -> ToolResult:
    spec = TOOLS.get(name)
    if not spec:
        return _error(f"未知工具：{name}")
    if not spec.read_only and read_env_file().get("AGENT_ALLOW_WRITE_TOOLS", "") != "true":
        return _error(f"工具 {name} 当前未启用（写操作需先开启 AGENT_ALLOW_WRITE_TOOLS）")

    args, problem = _prepare_args(spec, arguments)
    if problem:
        return _error(problem)

    try:
        async with get_session() as db:
            return await asyncio.wait_for(spec.handler(db, **args), timeout=spec.timeout)
    except asyncio.TimeoutError:
        return _error(f"工具 {name} 执行超时")
    except Exception as exc:  # 工具异常必须转成数据，不能中断对话
        return _error(f"{type(exc).__name__}: {exc}")
