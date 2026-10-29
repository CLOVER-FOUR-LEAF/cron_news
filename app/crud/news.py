from datetime import datetime, date, timedelta
import json
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import select, func, case, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import News
from app.models.news_tag import NewsTag
from app.schemas.news import NewsCreate
from app.services import tag_service
from app.services.text_utils import normalize_summary

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"
COVER_DIR = IMAGES_DIR / "cover"


async def get_news_list(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    keyword: str | None = None,
    tag: str | None = None,
    today_only: bool = False,
    date_filter: date | None = None,
    is_read: int | None = None,
    batch_id: str | None = None,
) -> tuple[list[News], int]:
    query = select(News).where(News.is_deleted == 0)

    relevance = None
    if keyword:
        terms = [t for t in re.split(r"[\s,，;；、]+", keyword) if t.strip()]
        if terms:
            conditions = [
                or_(
                    News.title.contains(t),
                    News.summary.contains(t),
                    News.content.contains(t),
                )
                for t in terms
            ]
            query = query.where(and_(*conditions))
            relevance = sum(case((News.title.contains(t), 1), else_=0) for t in terms)

    tag = tag_service.normalize_tag(tag or "")
    if tag:
        query = query.join(NewsTag, NewsTag.news_id == News.id).where(NewsTag.tag == tag)
    if batch_id:
        query = query.where(News.batch_id == batch_id)

    if is_read is not None:
        query = query.where(News.is_read == is_read)

    if today_only:
        today_start = datetime.combine(date.today(), datetime.min.time())
        query = query.where(News.collected_at >= today_start)
    elif date_filter:
        filter_start = datetime.combine(date_filter, datetime.min.time())
        filter_end = filter_start + timedelta(days=1)
        query = query.where(News.collected_at >= filter_start, News.collected_at < filter_end)

    count_query = select(func.count()).select_from(query.subquery())
    result = await db.execute(count_query)
    total = result.scalar()

    yesterday = datetime.combine(date.today() - timedelta(days=1), datetime.min.time())

    priority = case(
        (
            (News.is_read == 0) & (News.collected_at >= yesterday),
            0,
        ),
        (
            (News.is_read == 1) & (News.collected_at >= yesterday),
            1,
        ),
        else_=2,
    )

    if relevance is not None:
        query = query.order_by(relevance.desc(), News.collected_at.desc())
    else:
        query = query.order_by(priority, News.collected_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    items = list(result.scalars().all())

    return items, total


async def get_news_by_id(db: AsyncSession, news_id: int) -> News | None:
    query = select(News).where(News.id == news_id, News.is_deleted == 0)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_related_news(db: AsyncSession, news_id: int, limit: int = 10, recommend_enabled: bool = False) -> list[News]:
    news = await get_news_by_id(db, news_id)
    if not news:
        return []

    if recommend_enabled:
        ids = news.related_id_list
        if ids:
            result = await db.execute(
                select(News).where(News.id.in_(ids), News.is_deleted == 0)
            )
            items = {n.id: n for n in result.scalars().all()}
            ordered = [items[i] for i in ids if i in items]
            if ordered:
                return ordered[:limit]

    # 分类移除后，回退推荐改为「共享标签 + 时间/可信度」
    tags = tag_service.parse_tags(news)
    if tags:
        query = (
            select(News)
            .join(NewsTag, NewsTag.news_id == News.id)
            .where(News.is_deleted == 0, News.id != news_id, NewsTag.tag.in_(tags))
            .order_by(News.collected_at.desc())
            .limit(limit * 4)
        )
        candidates = list((await db.execute(query)).scalars().all())
        shared = []
        seen = set()
        for item in candidates:
            if item.id == news_id or item.id in seen:
                continue
            if set(tag_service.parse_tags(item)) & set(tags):
                shared.append(item)
                seen.add(item.id)
            if len(shared) >= limit:
                break
        if shared:
            return shared

    query = (
        select(News)
        .where(News.is_deleted == 0, News.id != news_id)
        .order_by(News.collected_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def create_news(db: AsyncSession, news_in: NewsCreate) -> News:
    tags = tag_service.normalize_tags(news_in.tags or [])
    summary = normalize_summary(news_in.summary, news_in.content)
    news = News(
        title=news_in.title,
        summary=summary or None,
        content=news_in.content,
        source_url=news_in.source_url,
        source=news_in.source,
        cover_url=news_in.cover_url,
        collected_at=news_in.collected_at or datetime.now(),
        tags=json.dumps(tags, ensure_ascii=False) if tags else None,
        credibility=news_in.credibility,
        analysis=json.dumps(news_in.analysis, ensure_ascii=False) if news_in.analysis else None,
    )
    db.add(news)
    await db.flush()
    await tag_service.sync_news_tags(db, news)
    await db.refresh(news)

    if news_in.cover_url:
        final = await finalize_cover(news, news_in.cover_url)
        if final:
            news.cover_url = final
            await db.flush()

    return news


async def finalize_cover(news: News, cover_url: str) -> str | None:
    """外部 Agent 提供的封面图落盘改名：把 images 目录下的图片改名保存到 cover/{新闻id}.{扩展名}。

    - 仅接受 `/images/` 开头的本地路径（外部 Agent 已把图片放进挂载的 images 目录，容器内可读）；
    - 校验路径位于 images 目录内，防止路径穿越；
    - 文件不存在时返回 None（保留原链接，由前端兜底），并确保返回的新链接真实存在。
    """
    if not cover_url or not cover_url.startswith("/images/"):
        return None
    images_dir = IMAGES_DIR.resolve()
    try:
        old_path = (IMAGES_DIR / cover_url[len("/images/"):]).resolve()
    except OSError:
        return None
    if not str(old_path).startswith(str(images_dir) + os.sep):
        return None
    if not old_path.is_file():
        return None
    try:
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        suffix = (old_path.suffix or ".png").lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            suffix = ".png"
        new_path = COVER_DIR / f"{news.id}{suffix}"
        shutil.move(str(old_path), str(new_path))
        return f"/images/cover/{news.id}{suffix}"
    except OSError:
        return None


async def mark_news_as_reading(db: AsyncSession, news_id: int) -> bool:
    news = await get_news_by_id(db, news_id)
    if not news:
        return False
    if news.is_read == 0:
        news.is_reading = 1
        if news.reading_at is None:
            news.reading_at = datetime.now()
    await db.flush()
    return True


async def mark_news_as_read(db: AsyncSession, news_id: int) -> bool:
    news = await get_news_by_id(db, news_id)
    if not news:
        return False
    news.is_read = 1
    news.is_reading = 0
    if news.read_at is None:
        news.read_at = datetime.now()
    if news.is_later:
        news.is_later = 0
        news.later_at = None
    await db.flush()
    return True


async def get_news_stats(db: AsyncSession) -> dict:
    today_start = datetime.combine(date.today(), datetime.min.time())

    total = int(await db.scalar(select(func.count()).where(News.is_deleted == 0)) or 0)
    read_count = int(
        await db.scalar(select(func.count()).where(News.is_deleted == 0, News.is_read == 1)) or 0
    )
    unread_count = total - read_count
    today_count = int(
        await db.scalar(
            select(func.count()).where(News.is_deleted == 0, News.collected_at >= today_start)
        )
        or 0
    )

    # 标签统计：一条新闻可计入多个标签，因此 tagged_total 与 tag_assignments_total 分开
    tag_rows = (
        await db.execute(
            select(NewsTag.tag, func.count(func.distinct(NewsTag.news_id)))
            .join(News, News.id == NewsTag.news_id)
            .where(News.is_deleted == 0)
            .group_by(NewsTag.tag)
            .order_by(func.count(func.distinct(NewsTag.news_id)).desc(), NewsTag.tag)
        )
    ).all()
    tag_stats = [
        {"tag": tag, "count": int(count), "color": tag_service.tag_color(tag)}
        for tag, count in tag_rows
    ]
    tagged_total = int(
        await db.scalar(
            select(func.count(func.distinct(NewsTag.news_id)))
            .join(News, News.id == NewsTag.news_id)
            .where(News.is_deleted == 0)
        )
        or 0
    )
    tag_assignments_total = int(
        await db.scalar(
            select(func.count())
            .select_from(NewsTag)
            .join(News, News.id == NewsTag.news_id)
            .where(News.is_deleted == 0)
        )
        or 0
    )
    untagged_count = max(0, total - tagged_total)

    range_start = datetime.combine(date.today() - timedelta(days=29), datetime.min.time())
    daily_rows = (
        await db.execute(
            select(func.date(News.collected_at), func.count(News.id))
            .where(News.is_deleted == 0, News.collected_at >= range_start)
            .group_by(func.date(News.collected_at))
        )
    ).all()
    daily_counts = {str(day): int(count) for day, count in daily_rows if day}

    primary_rows = (
        await db.execute(
            select(func.date(News.collected_at), NewsTag.tag, func.count(News.id))
            .join(NewsTag, NewsTag.news_id == News.id)
            .where(
                News.is_deleted == 0,
                News.collected_at >= range_start,
                NewsTag.position == 0,
            )
            .group_by(func.date(News.collected_at), NewsTag.tag)
        )
    ).all()
    primary_map: dict[str, dict[str, int]] = {}
    for day, tag, count in primary_rows:
        if not day:
            continue
        primary_map.setdefault(str(day), {})[tag] = int(count)

    daily_stats = []
    for i in range(29, -1, -1):
        day_str = (date.today() - timedelta(days=i)).isoformat()
        by_primary_tag = dict(primary_map.get(day_str, {}))
        count = daily_counts.get(day_str, sum(by_primary_tag.values()))
        missing = count - sum(by_primary_tag.values())
        if missing > 0:
            by_primary_tag["未标记"] = missing
        daily_stats.append(
            {
                "date": day_str,
                "count": count,
                "by_primary_tag": by_primary_tag,
            }
        )

    read_by_day = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(func.date(News.read_at), func.count(News.id))
                .where(News.is_deleted == 0, News.read_at.isnot(None))
                .group_by(func.date(News.read_at))
            )
        ).all()
        if row[0]
    }

    recent_rows = (
        await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_read == 1)
            .order_by(News.read_at.desc())
            .limit(10)
        )
    ).scalars().all()
    reading_rows = (
        await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_reading == 1, News.is_read == 0)
            .order_by(News.reading_at.desc())
            .limit(10)
        )
    ).scalars().all()
    tag_map = await tag_service.news_tags_map(
        db, [row.id for row in list(recent_rows) + list(reading_rows)]
    )

    def _stat_item(news: News) -> dict:
        tags = tag_map.get(news.id) or tag_service.parse_tags(news)
        primary = tags[0] if tags else "未标记"
        return {
            "id": news.id,
            "title": news.title,
            "short_summary": news.short_summary,
            "tags": tags,
            "primary_tag": primary,
            "primary_color": tag_service.tag_color(primary) if tags else "#8a8690",
            "source": news.source,
            "collected_at": news.collected_at.isoformat() if news.collected_at else None,
            "read_at": news.read_at.isoformat() if news.read_at else None,
            "reading_at": news.reading_at.isoformat() if news.reading_at else None,
            "is_read": news.is_read,
            "is_reading": news.is_reading,
        }

    return {
        "total": total,
        "read_count": read_count,
        "unread_count": unread_count,
        "today_count": today_count,
        "tag_stats": tag_stats,
        "tagged_total": tagged_total,
        "untagged_count": untagged_count,
        "tag_assignments_total": tag_assignments_total,
        "daily_stats": daily_stats,
        "read_by_day": read_by_day,
        "recent_reads": [_stat_item(news) for news in recent_rows],
        "recent_reading": [_stat_item(news) for news in reading_rows],
    }


async def get_recommended_news(db: AsyncSession, mode: str, limit: int = 10) -> dict:
    """个性化推荐已迁移到 recommend_service（行为统计 + 反馈 + 多样性）。"""
    from app.services.recommend_service import personalized_recommendations

    return await personalized_recommendations(db, limit, mode=mode)


async def toggle_news_flag(db: AsyncSession, news_id: int, flag: str) -> int | None:
    news = await get_news_by_id(db, news_id)
    if not news:
        return None
    now = datetime.now()
    if flag == "fav":
        news.is_fav = 0 if news.is_fav else 1
        news.fav_at = now if news.is_fav else None
        value = news.is_fav
    else:
        news.is_later = 0 if news.is_later else 1
        news.later_at = now if news.is_later else None
        value = news.is_later
    await db.flush()
    return value


async def get_my_list(db: AsyncSession, kind: str) -> list[News]:
    query = select(News).where(News.is_deleted == 0)
    if kind == "favorites":
        query = query.where(News.is_fav == 1).order_by(News.fav_at.desc())
    elif kind == "later":
        query = query.where(News.is_later == 1).order_by(News.later_at.desc())
    else:
        return []
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_my_counts(db: AsyncSession) -> dict:
    result = await db.execute(select(func.count()).where(News.is_deleted == 0, News.is_fav == 1))
    fav_count = result.scalar()
    result = await db.execute(select(func.count()).where(News.is_deleted == 0, News.is_later == 1))
    later_count = result.scalar()
    return {"fav_count": fav_count, "later_count": later_count}
