"""标签体系：新闻标签归一化、颜色、聚合、关注与批次查询。

分类概念已移除；一篇新闻可以有多个标签，标签由采集 Agent 在结构化分析时自由生成，
用户可以关注标签，搜索 Agent 会优围绕关注的标签采集。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import News, NewsTag, TagFollow

TAG_PALETTE = [
    "#d4a853", "#4a8fd4", "#c75050", "#4aad6a", "#c47ddc", "#e88b5a",
    "#5aa0a0", "#b07a4a", "#7a8fd4", "#b45a8a", "#6aa84f", "#c9a227",
]
GENERIC_TAGS = {
    "新闻", "资讯", "最新", "报道", "快讯", "快报", "内容", "综合", "热点",
    "头条", "要闻", "推荐", "文章", "消息", "动态", "其他", "其它",
}


def normalize_tag(tag: str) -> str:
    value = unicodedata.normalize("NFKC", str(tag or "")).strip()
    value = re.sub(r"^[#＃\s]+", "", value)
    value = re.sub(r"^[\-\*•·]+\s*", "", value)
    value = value.strip(" \t\r\n\"'“”‘’《》<>[]【】()（）")
    value = re.sub(r"[\\/]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    value = value[:30].strip()
    return value


def normalize_tags(tags: Any, limit: int = 8) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[,，、;；|/\s]+", tags)
    if not isinstance(tags, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        parts = re.split(r"[,，、;；|]+", str(raw or "")) if isinstance(raw, str) else [str(raw or "")]
        for part in parts:
            _append_tag(part, result, seen, limit)
            if len(result) >= limit:
                return result
    return result


def _append_tag(raw: str, result: list[str], seen: set[str], limit: int) -> None:
    tag = normalize_tag(raw)
    if not tag:
        return
    if tag.lower() in GENERIC_TAGS or tag in GENERIC_TAGS:
        return
    key = tag.casefold()
    if key in seen:
        return
    seen.add(key)
    result.append(tag)


def tag_color(tag: str) -> str:
    digest = hashlib.md5((tag or "").encode("utf-8")).hexdigest()
    return TAG_PALETTE[int(digest[:8], 16) % len(TAG_PALETTE)]


def parse_tags(news_or_tags: Any) -> list[str]:
    if isinstance(news_or_tags, News):
        return normalize_tags(news_or_tags.tag_list)
    if isinstance(news_or_tags, str):
        try:
            parsed = json.loads(news_or_tags)
        except (json.JSONDecodeError, TypeError):
            return normalize_tags(news_or_tags)
        return normalize_tags(parsed)
    return normalize_tags(news_or_tags)


async def followed_tags(db: AsyncSession) -> list[str]:
    rows = (
        await db.execute(select(TagFollow).order_by(TagFollow.id))
    ).scalars().all()
    return [row.tag for row in rows]


async def sync_news_tags(db: AsyncSession, news: News) -> list[str]:
    """把 news.tags（展示事实来源）同步到 news_tags 索引表。"""
    if news.id is None:
        await db.flush()
    tags = parse_tags(news)
    await db.execute(delete(NewsTag).where(NewsTag.news_id == news.id))
    for position, tag in enumerate(tags):
        db.add(NewsTag(news_id=news.id, tag=tag, position=position))
    await db.flush()
    return tags


async def sync_all_news_tags(db: AsyncSession) -> int:
    await db.execute(delete(NewsTag))
    rows = (
        await db.execute(select(News.id, News.tags).where(News.is_deleted == 0))
    ).all()
    total = 0
    for news_id, raw_tags in rows:
        for position, tag in enumerate(parse_tags(raw_tags)):
            db.add(NewsTag(news_id=int(news_id), tag=tag, position=position))
            total += 1
    await db.flush()
    return total


async def migrate_categories_to_tags(db: AsyncSession) -> int:
    """把旧库的 category.name 迁成主标签；仅有 categories 表存在时执行。"""
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT n.id, c.name FROM news n JOIN categories c ON n.category_id = c.id "
                    "WHERE n.is_deleted = 0 AND (n.tags IS NULL OR n.tags = '' OR n.tags = '[]')"
                )
            )
        ).all()
    except Exception:
        return 0
    if not rows:
        return 0
    for news_id, name in rows:
        tag = normalize_tag(name)
        if not tag:
            continue
        news = (await db.execute(select(News).where(News.id == int(news_id)))).scalars().first()
        if not news:
            continue
        news.tags = json.dumps([tag], ensure_ascii=False)
        await sync_news_tags(db, news)
    await db.flush()
    return len(rows)


async def set_follow(db: AsyncSession, tag: str, follow: bool) -> tuple[str, bool]:
    tag = normalize_tag(tag)
    if not tag:
        raise ValueError("标签不能为空")
    existing = (
        await db.execute(select(TagFollow).where(TagFollow.tag == tag))
    ).scalars().first()
    if follow and not existing:
        db.add(TagFollow(tag=tag, source="user"))
    elif not follow and existing:
        await db.delete(existing)
    await db.flush()
    return tag, follow


async def latest_batch_id(db: AsyncSession) -> str:
    row = (
        await db.execute(
            select(News.batch_id)
            .where(News.is_deleted == 0, News.batch_id.isnot(None), News.batch_id != "")
            .order_by(News.collected_at.desc())
            .limit(1)
        )
    ).first()
    return str(row[0]) if row and row[0] else ""


async def batch_news(
    db: AsyncSession,
    *,
    batch_id: str | None = None,
    limit: int = 100,
    unread_only: bool = False,
) -> tuple[str, list[News]]:
    """返回 (batch_id, items)。没有批次号时回退到最近 24 小时。"""
    resolved = batch_id or await latest_batch_id(db)
    query = select(News).where(News.is_deleted == 0)
    if resolved:
        query = query.where(News.batch_id == resolved)
    else:
        query = query.where(News.collected_at >= datetime.now() - timedelta(hours=24))
    if unread_only:
        query = query.where(News.is_read == 0)
    query = query.order_by(News.collected_at.desc()).limit(max(1, min(limit, 300)))
    items = list((await db.execute(query)).scalars().all())
    return resolved, items


def count_tags(items: list[News], *, limit: int = 30) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for news in items:
        for tag in parse_tags(news):
            counts[tag] = counts.get(tag, 0) + 1
            first_seen.setdefault(tag, news.collected_at.isoformat() if news.collected_at else "")
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"tag": tag, "count": count, "color": tag_color(tag), "last_collected": first_seen.get(tag)}
        for tag, count in ordered[:limit]
    ]


async def top_tags(
    db: AsyncSession,
    *,
    days: int | None = None,
    batch_id: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    query = (
        select(
            NewsTag.tag,
            func.count(func.distinct(NewsTag.news_id)),
            func.max(News.collected_at),
        )
        .join(News, News.id == NewsTag.news_id)
        .where(News.is_deleted == 0)
    )
    if batch_id:
        query = query.where(News.batch_id == batch_id)
    if days:
        query = query.where(News.collected_at >= datetime.now() - timedelta(days=days))
    query = (
        query.group_by(NewsTag.tag)
        .order_by(func.count(func.distinct(NewsTag.news_id)).desc(), NewsTag.tag)
        .limit(max(1, min(limit, 200)))
    )
    rows = (await db.execute(query)).all()
    return [
        {
            "tag": tag,
            "count": int(count),
            "color": tag_color(tag),
            "last_collected": last.isoformat() if last else None,
        }
        for tag, count, last in rows
    ]


async def tag_news(
    db: AsyncSession,
    tag: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[News], int]:
    tag = normalize_tag(tag)
    if not tag:
        return [], 0
    base = (
        select(News.id)
        .join(NewsTag, NewsTag.news_id == News.id)
        .where(News.is_deleted == 0, NewsTag.tag == tag)
    )
    total = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    ids = [
        int(item)
        for item in (
            await db.execute(
                base.order_by(News.collected_at.desc())
                .offset(max(0, offset))
                .limit(max(1, min(limit, 100)))
            )
        ).scalars().all()
    ]
    if not ids:
        return [], total
    rows = (
        await db.execute(select(News).where(News.id.in_(ids)))
    ).scalars().all()
    by_id = {news.id: news for news in rows}
    return [by_id[item] for item in ids if item in by_id], total


async def news_tags_map(db: AsyncSession, news_ids: list[int]) -> dict[int, list[str]]:
    if not news_ids:
        return {}
    rows = (
        await db.execute(
            select(NewsTag.news_id, NewsTag.tag)
            .where(NewsTag.news_id.in_(news_ids))
            .order_by(NewsTag.news_id, NewsTag.position)
        )
    ).all()
    result: dict[int, list[str]] = {}
    for news_id, tag in rows:
        result.setdefault(int(news_id), []).append(tag)
    return result


async def tag_overview(db: AsyncSession, *, batch_id: str | None = None) -> dict[str, Any]:
    resolved = batch_id or await latest_batch_id(db)
    tags = await top_tags(db, batch_id=resolved or None, days=None if resolved else 30, limit=40)
    followed = set(await followed_tags(db))
    for item in tags:
        item["followed"] = item["tag"] in followed
    for tag in followed:
        if tag not in {item["tag"] for item in tags}:
            tags.append({"tag": tag, "count": 0, "color": tag_color(tag), "followed": True})
    return {"batch_id": resolved, "items": tags, "followed": sorted(followed)}


def tag_terms(news: News) -> list[str]:
    return parse_tags(news)
