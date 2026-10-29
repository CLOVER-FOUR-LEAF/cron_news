"""Agent 工具注册表。

- 只读工具：检索、读新闻/简报、统计、收藏等
- 委派工具：把采集/简报/推荐任务交给对应的专职 Agent（见 agent_jobs）

工具结果统一为 {ok, summary, count, data}，异常一律转成 {ok:false, error}
回传给模型，绝不让异常冒泡中断对话。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import news as news_crud
from app.database import get_session
from app.env_store import read_env_file
from app.services import (
    brief_service,
    feedback_service,
    memory_service,
    recommend_service,
    retrieval,
    tag_service,
    user_profile,
)


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
    # read=只读；write=修改用户数据（需显式开启）；delegate=委派给其它 Agent
    kind: str = "read"
    timeout: float = 20.0

    @property
    def read_only(self) -> bool:
        return self.kind == "read"


def _delegation_enabled() -> bool:
    raw = (read_env_file().get("AGENT_ALLOW_DELEGATION", "") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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
    tag: str | None = None,
    days: int | None = None,
    limit: int = 8,
) -> ToolResult:
    limit = _as_int(limit, 8, 1, 20)
    day_value = _as_int(days, 1, 1, 365) if days is not None else None
    result = await retrieval.search_news(db, query, tag=tag, days=day_value, limit=limit)
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


async def _timeline_news(
    db: AsyncSession,
    query: str,
    range: str = "过去半个月",
    tag: str | None = None,
    max_events: int = 50,
) -> ToolResult:
    from app.services import time_range

    parsed = time_range.parse_time_range(range)
    max_events = _as_int(max_events, 50, 5, 100)
    result = await retrieval.search_timeline(
        db,
        query,
        since=parsed["since"],
        until=parsed["until"],
        tag=tag,
        bucket_days=parsed["bucket_days"],
        max_events=max_events,
    )
    items = result.get("items") or []
    coverage = result.get("coverage") or {}
    summary = (
        f"在{parsed['label']}内找到 {len(items)} 条相关事件，"
        f"覆盖 {coverage.get('days_with_news', 0)}/{coverage.get('days_total', parsed['days'])} 天"
    )
    return ToolResult(
        ok=True,
        data={
            "range": result.get("range"),
            "timeline": result.get("timeline"),
            "buckets": result.get("buckets"),
            "coverage": coverage,
        },
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
    tag: str | None = None,
    days: int = 1,
    unread_only: bool = False,
    limit: int = 10,
) -> ToolResult:
    days = _as_int(days, 1, 1, 90)
    limit = _as_int(limit, 10, 1, 30)
    unread_only = _as_bool(unread_only)
    result = await retrieval.list_recent(
        db, tag=tag, days=days, unread_only=unread_only, limit=limit
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


async def _get_daily_brief(db: AsyncSession, tag: str | None = None, date: str | None = None) -> ToolResult:
    if tag and tag not in {"每日", "daily"}:
        brief = (
            await brief_service.get_brief_by_date(db, tag, date)
            if date
            else await brief_service.get_latest_brief(db, tag)
        )
        if not brief:
            return ToolResult(ok=True, data=None, summary=f"「{tag}」暂无简报", count=0)
        return ToolResult(
            ok=True,
            data={
                "tag": brief.category_name,
                "date": brief.brief_date,
                "content": (brief.content or "")[:4000],
                "source": brief.source,
            },
            summary=f"读取「{tag}」{brief.brief_date} 简报",
            count=1,
        )

    brief = await brief_service.get_daily_brief(db, date)
    if not brief:
        return ToolResult(ok=True, data=None, summary="暂无每日简报", count=0)
    return ToolResult(
        ok=True,
        data={
            "date": brief.brief_date,
            "content": (brief.content or "")[:4000],
            "source": brief.source,
        },
        summary=f"读取 {brief.brief_date} 每日简报",
        count=1,
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
        "tag_stats": stats.get("tag_stats"),
        "tagged_total": stats.get("tagged_total"),
        "recent_reads": [
            {"id": item["id"], "title": item["title"], "tags": item.get("tags")}
            for item in (stats.get("recent_reads") or [])[:5]
        ],
    }
    return ToolResult(
        ok=True,
        data=data,
        summary=f"新闻库共 {data['total']} 条，未读 {data['unread_count']} 条，今日新增 {data['today_count']} 条",
        count=1,
    )


async def _list_tags(db: AsyncSession, limit: int = 30) -> ToolResult:
    limit = _as_int(limit, 30, 1, 80)
    items = await tag_service.top_tags(db, days=90, limit=limit)
    followed = set(await tag_service.followed_tags(db))
    for item in items:
        item["followed"] = item["tag"] in followed
    return ToolResult(
        ok=True,
        data={"items": items, "followed": sorted(followed)},
        summary=f"共 {len(items)} 个标签",
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


async def _read_memory(db: AsyncSession) -> ToolResult:
    if not memory_service.enabled():
        return _error("记忆系统未启用")
    try:
        await memory_service.ensure_profile_fresh(db)
        content = await memory_service.read_memory()
    except Exception as exc:
        return _error(f"读取记忆失败：{exc}")
    sections = memory_service.sections(content)
    compact = {
        key: value[:500]
        for key, value in sections.items()
        if value and value.strip() and "（暂无）" not in value
    }
    return ToolResult(
        ok=True,
        data={"content": content[:3000], "sections": compact},
        summary="读取长期记忆（用户画像 / 兴趣 / 跟踪任务 / 反馈整改）",
        count=1,
    )


async def _remember(db: AsyncSession, content: str, section: str = "notes") -> ToolResult:
    if not memory_service.enabled():
        return _error("记忆系统未启用")
    try:
        updated = await memory_service.remember(str(content or ""), str(section or "notes"))
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"写入记忆失败：{exc}")
    return ToolResult(
        ok=True,
        data={"section": section, "content": updated},
        summary=f"已写入记忆「{section}」",
        count=1,
    )


async def _forget_memory(db: AsyncSession, query: str) -> ToolResult:
    if not memory_service.enabled():
        return _error("记忆系统未启用")
    try:
        removed = await memory_service.forget(str(query or ""))
    except Exception as exc:
        return _error(f"删除记忆失败：{exc}")
    return ToolResult(
        ok=True,
        data={"removed": removed},
        summary=f"已删除 {removed} 条相关记忆" if removed else "没有找到匹配的记忆",
        count=removed,
    )


async def _get_user_profile(db: AsyncSession) -> ToolResult:
    try:
        profile = await user_profile.compute_profile(db)
    except Exception as exc:
        return _error(f"读取用户画像失败：{exc}")
    summary = user_profile.profile_summary(profile)
    top_tags = "、".join(summary.get("top_tags") or []) or "暂无"
    return ToolResult(
        ok=True,
        data=summary,
        summary=f"阅读 {summary['totals'].get('read', 0)} 条，偏好标签：{top_tags}",
        count=1,
    )


async def _get_recommendations(db: AsyncSession, limit: int = 8) -> ToolResult:
    limit = _as_int(limit, 8, 1, 15)
    result = await recommend_service.personalized_recommendations(db, limit)
    items = result.get("items") or []
    return ToolResult(
        ok=True,
        data={
            "hint": result.get("hint"),
            "items": items,
        },
        summary=f"按你的习惯挑选了 {len(items)} 条未读新闻",
        count=len(items),
        refs=retrieval.refs_from_items(items),
    )


async def _submit_feedback(
    db: AsyncSession,
    target_type: str = "chat",
    target_id: int | None = None,
    rating: int = -1,
    reason: str = "",
    comment: str = "",
    conversation_id: int | None = None,
) -> ToolResult:
    target_type = str(target_type or "chat").strip().lower()
    target_id = _as_int(target_id, 0, 0, 2_000_000_000) or None
    rating = _as_int(rating, -1, -1, 1)
    try:
        feedback = await feedback_service.create_feedback(
            db,
            target_type=target_type,
            target_id=target_id,
            conversation_id=conversation_id,
            rating=rating,
            reason=str(reason or ""),
            comment=str(comment or ""),
        )
        await db.commit()
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"记录反馈失败：{exc}")
    feedback_service.schedule_reflection()
    return ToolResult(
        ok=True,
        data={"id": feedback.id, "status": feedback.status},
        summary="反馈已记录，并会用于调整后续采集、推荐与回答方式",
        count=1,
    )


async def _search_web(db: AsyncSession, query: str, hours: int = 24, max_results: int = 8) -> ToolResult:
    from app.services import content_quality
    from app.services.ai_service import search_news as web_search

    hours = _as_int(hours, 24, 1, 720)
    max_results = _as_int(max_results, 8, 1, 10)
    hits = await web_search(db, query, max_results=max_results, hours=hours)
    items = []
    refs = {}
    skipped = 0
    for hit in hits:
        url = hit.get("url") or hit.get("link") or ""
        title = str(hit.get("title") or "")
        snippet = hit.get("snippet") or hit.get("description") or ""
        ok, _reason = content_quality.is_probable_article(title, url, snippet)
        if not url or not ok:
            skipped += 1
            continue
        ref = f"web:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}"
        item = {
            "ref": ref,
            "title": content_quality.clean_title(title),
            "snippet": str(snippet)[:240],
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
    summary = f"网络搜索返回 {len(items)} 条可用结果"
    if skipped:
        summary += f"（已过滤 {skipped} 条门户/登录/导航页）"
    return ToolResult(
        ok=True,
        data={"items": items},
        summary=summary,
        count=len(items),
        refs=refs,
    )


async def _delegate_collect(
    db: AsyncSession,
    tags: list[str] | None = None,
    hours: int | None = None,
    __emit=None,
) -> ToolResult:
    """把采集任务委派给采集 Agent。"""
    from app.services import agent_jobs, tag_service

    names = None
    if tags:
        if isinstance(tags, str):
            tags = [tags]
        names = tag_service.normalize_tags(tags)

    result = await agent_jobs.run_collection(
        emit=(__emit if __emit else None), tags=names, interval_hours=hours
    )
    if not result.get("ok"):
        if result.get("busy"):
            return ToolResult(
                ok=False,
                error=result.get("message") or "采集 Agent 正忙",
                summary="采集 Agent 正忙",
            )
        return ToolResult(ok=False, error=result.get("error") or "采集失败", summary="采集失败")

    data = result.get("result") or {}
    total = int(data.get("total_new") or 0)
    details = data.get("details") or {}
    parts = [f"{name} {count} 条" for name, count in details.items() if isinstance(count, int)]
    detail_text = "；".join(parts) if parts else "无标签明细"
    elapsed = result.get("elapsed_seconds")
    return ToolResult(
        ok=True,
        data={"total_new": total, "details": details, "elapsed_seconds": elapsed},
        summary=f"采集 Agent 完成：新增 {total} 条新闻（{detail_text}）",
        count=total,
    )


async def _delegate_brief(db: AsyncSession, __emit=None) -> ToolResult:
    """把简报生成任务委派给简报 Agent。"""
    from app.services import agent_jobs

    result = await agent_jobs.run_brief(emit=(__emit if __emit else None))
    if not result.get("ok"):
        if result.get("busy"):
            return ToolResult(ok=False, error=result.get("message") or "简报 Agent 正忙", summary="简报 Agent 正忙")
        return ToolResult(ok=False, error=result.get("error") or "简报生成失败", summary="简报生成失败")

    data = result.get("result") or {}
    generated = int(data.get("generated") or 0)
    return ToolResult(
        ok=True,
        data={"generated": generated, "elapsed_seconds": result.get("elapsed_seconds")},
        summary="今日简报已生成" if generated else "今日暂无简报",
        count=generated,
    )


async def _delegate_recommend(db: AsyncSession, __emit=None) -> ToolResult:
    """把相关性计算任务委派给推荐 Agent。"""
    from app.services import agent_jobs

    result = await agent_jobs.run_recommend(emit=(__emit if __emit else None))
    if not result.get("ok"):
        if result.get("busy"):
            return ToolResult(ok=False, error=result.get("message") or "推荐 Agent 正忙", summary="推荐 Agent 正忙")
        return ToolResult(ok=False, error=result.get("error") or "推荐计算失败", summary="推荐计算失败")

    data = result.get("result") or {}
    updated = int(data.get("updated") or 0)
    return ToolResult(
        ok=True,
        data={"updated": updated, "elapsed_seconds": result.get("elapsed_seconds")},
        summary=f"推荐 Agent 完成：更新 {updated} 条新闻的相关性",
        count=updated,
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
                "tag": {"type": "string", "description": "可选标签筛选，如 AI芯片、地缘政治"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
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
        description="按标签、时间、已读状态浏览新闻，按收录时间倒序。适合「今天有什么」「最近 AI 新闻」这类问题。",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 1},
                "unread_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
        },
        handler=_list_news,
    ),
    "get_daily_brief": ToolSpec(
        name="get_daily_brief",
        description="读取每日简报（Markdown）。默认返回最新一期统一简报；可传 date 指定日期。",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "兼容旧数据；一般不用传"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
        handler=_get_daily_brief,
    ),
    "get_related_news": ToolSpec(
        name="get_related_news",
        description="读取与某条新闻相关的其它新闻（平台已计算的跨标签关联；未计算时回退共享标签最新）。",
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
        description="获取新闻库统计：总数、已读/未读、今日新增、标签分布、最近阅读。用于趋势和概览类问题。",
        parameters={"type": "object", "properties": {}},
        handler=_get_news_stats,
    ),
    "list_tags": ToolSpec(
        name="list_tags",
        description="列出新闻库中的标签、数量、颜色与用户是否已关注。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 80, "default": 30}
            },
        },
        handler=_list_tags,
    ),
    "timeline_news": ToolSpec(
        name="timeline_news",
        description=(
            "按时间线梳理新闻，适合「过去半个月 AI 发展历程」「最近一个月某主题的演变」这类问题。"
            "会按天/周分桶召回、跨来源去重，并返回覆盖情况。回答时按日期组织并引用每条事件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "主题 / 实体 / 关键词"},
                "range": {
                    "type": "string",
                    "description": "中文时间范围，如 过去半个月、最近两周、上个月、2026-09-01 到 2026-09-15",
                    "default": "过去半个月",
                },
                "tag": {"type": "string", "description": "可选标签筛选"},
                "max_events": {"type": "integer", "minimum": 5, "maximum": 100, "default": 50},
            },
            "required": ["query"],
        },
        handler=_timeline_news,
        timeout=60.0,
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
    "read_memory": ToolSpec(
        name="read_memory",
        description=(
            "读取你的长期记忆：用户画像、兴趣、跟踪任务、阅读偏好、需要避免的内容、反馈整改。"
            "当用户问「你记得我什么」「我的偏好吗」，或需要确认记忆时调用。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_read_memory,
        kind="read",
    ),
    "remember": ToolSpec(
        name="remember",
        description=(
            "把用户明确要求记住的信息写入长期记忆（所有 Agent 共用）。"
            "用户说「记住 / 以后多给我… / 跟踪… / 不要再… / 我喜欢…」时必须调用，并在回答里简短确认。"
            "section 选择：profile=身份与背景，interests=兴趣主题，watch=跟踪任务，"
            "preferences=阅读偏好，avoid=需要避免，notes=其他笔记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "一条简洁、可执行的记忆，不超过 200 字"},
                "section": {
                    "type": "string",
                    "enum": ["profile", "interests", "watch", "preferences", "avoid", "notes"],
                    "default": "notes",
                },
            },
            "required": ["content"],
        },
        handler=_remember,
        kind="memory",
    ),
    "forget_memory": ToolSpec(
        name="forget_memory",
        description="删除长期记忆中与 query 匹配的条目。用户说「忘掉…」「不要再记着…」时调用。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "要删除的记忆关键词"}},
            "required": ["query"],
        },
        handler=_forget_memory,
        kind="memory",
    ),
    "get_user_profile": ToolSpec(
        name="get_user_profile",
        description=(
            "读取基于真实行为计算的用户画像：已读/收藏数量、偏好标签、常看来源、关注话题、"
            "最近读过什么、负反馈原因。适合回答「我喜欢什么」「我最近在看什么」这类问题。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_get_user_profile,
        kind="read",
    ),
    "get_recommendations": ToolSpec(
        name="get_recommendations",
        description=(
            "按用户阅读习惯、收藏、停留时长和反馈，推荐当前未读新闻，并返回每条推荐的理由。"
            "用户说「给我推荐点新闻」「有什么值得看的」时优先调用，而不是随机列新闻。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8}
            },
        },
        handler=_get_recommendations,
        kind="read",
    ),
    "submit_feedback": ToolSpec(
        name="submit_feedback",
        description=(
            "记录用户对回答或新闻的反馈（满意/不满意 + 原因）。"
            "用户抱怨「不相关 / 太浅 / 重复 / 过时 / 有错误 / 来源不可靠」时调用，"
            "并说明后续会如何调整。不要只道歉。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_type": {
                    "type": "string",
                    "enum": ["news", "message", "recommend", "chat"],
                    "default": "chat",
                },
                "target_id": {"type": "integer", "description": "新闻 ID 或助手消息 ID，可留空"},
                "rating": {"type": "integer", "enum": [-1, 0, 1], "default": -1},
                "reason": {
                    "type": "string",
                    "description": "irrelevant / shallow / duplicate / outdated / inaccurate / source / too_long / helpful / other",
                },
                "comment": {"type": "string", "description": "用户的原话或补充说明"},
            },
        },
        handler=_submit_feedback,
        kind="memory",
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
    "delegate_collect": ToolSpec(
        name="delegate_collect",
        description=(
            "把「采集新闻」任务委派给采集 Agent。用户说“帮我采集一下新闻/抓取最新资讯/更新新闻库”时调用。"
            "采集 Agent 会联网搜索、撰写正文、生成封面并建立向量索引，可能需要一到数分钟。"
            "完成后把新增条数汇报给用户，不要自己编造采集结果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，只围绕这些标签采集；不传则沿用关注标签与长期记忆",
                },
                "hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 72,
                    "description": "可选，回溯多少小时；不传则沿用任务配置",
                },
            },
        },
        handler=_delegate_collect,
        kind="delegate",
        timeout=900.0,
    ),
    "delegate_brief": ToolSpec(
        name="delegate_brief",
        description=(
            "把「生成每日简报」任务委派给简报 Agent。用户说“给我一份简报/总结今天的新闻/出一份日报”时调用。"
            "简报 Agent 会把当日全部新闻汇总成一篇统一的每日简报并写入简报页。"
            "完成后告知已生成，并可用 get_daily_brief 读取正文。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_delegate_brief,
        kind="delegate",
        timeout=600.0,
    ),
    "delegate_recommend": ToolSpec(
        name="delegate_recommend",
        description=(
            "把「计算相关新闻」任务委派给推荐 Agent。用户要求“更新相关推荐/重新计算新闻关联”时调用。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_delegate_recommend,
        kind="delegate",
        timeout=600.0,
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


async def execute_tool(name: str, arguments: Any, emit=None) -> ToolResult:
    """执行工具。

    emit 可选，用于委派类工具上报长任务进度（如“采集 Agent 正在搜索…”）。
    """
    spec = TOOLS.get(name)
    if not spec:
        return _error(f"未知工具：{name}")
    if spec.kind == "write" and read_env_file().get("AGENT_ALLOW_WRITE_TOOLS", "") != "true":
        return _error(f"工具 {name} 当前未启用（写操作需先开启 AGENT_ALLOW_WRITE_TOOLS）")
    if spec.kind == "memory" and not memory_service.enabled():
        return _error(f"工具 {name} 当前未启用（记忆系统已关闭）")
    if spec.kind == "delegate" and not _delegation_enabled():
        return _error(f"工具 {name} 当前未启用（Agent 委派需开启 AGENT_ALLOW_DELEGATION）")

    args, problem = _prepare_args(spec, arguments)
    if problem:
        return _error(problem)

    try:
        if spec.kind == "delegate":
            # 委派任务自己会开 session，不要在这里额外占一个
            if emit is not None:
                async def forward(event_type: str, text: str, **extra: Any):
                    await emit(text)

                return await asyncio.wait_for(
                    spec.handler(None, __emit=forward, **args), timeout=spec.timeout
                )
            return await asyncio.wait_for(
                spec.handler(None, **args), timeout=spec.timeout
            )

        async with get_session() as db:
            return await asyncio.wait_for(spec.handler(db, **args), timeout=spec.timeout)
    except asyncio.TimeoutError:
        return _error(f"工具 {name} 执行超时")
    except Exception as exc:  # 工具异常必须转成数据，不能中断对话
        return _error(f"{type(exc).__name__}: {exc}")


def is_delegate(name: str) -> bool:
    spec = TOOLS.get(name)
    return bool(spec and spec.kind == "delegate")
