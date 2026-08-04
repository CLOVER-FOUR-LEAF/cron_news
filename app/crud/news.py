from datetime import datetime, date, timedelta
import os
import random
import shutil
from pathlib import Path

from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import News
from app.models.category import Category
from app.schemas.news import NewsCreate

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IMAGES_DIR = BASE_DIR / "images"


async def get_news_list(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    keyword: str | None = None,
    category: str | None = None,
    today_only: bool = False,
    date_filter: date | None = None,
    is_read: int | None = None,
) -> tuple[list[News], int]:
    query = select(News).where(News.is_deleted == 0)

    if keyword:
        query = query.where(News.title.contains(keyword))

    if category:
        query = query.join(Category).where(Category.name == category)

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

    query = query.order_by(priority, News.collected_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    items = result.scalars().all()

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

    query = (
        select(News)
        .where(News.is_deleted == 0, News.id != news_id, News.category_id == news.category_id)
        .order_by(News.collected_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def create_news(db: AsyncSession, news_in: NewsCreate) -> News:
    news = News(
        title=news_in.title,
        summary=news_in.summary,
        content=news_in.content,
        source_url=news_in.source_url,
        source=news_in.source,
        category_id=news_in.category_id,
        cover_url=news_in.cover_url,
        collected_at=news_in.collected_at or datetime.now(),
    )
    db.add(news)
    await db.flush()
    await db.refresh(news)

    if news_in.cover_url:
        old_path = BASE_DIR / news_in.cover_url.lstrip('/')
        if old_path.exists():
            ext = old_path.suffix
            new_filename = f"{news.id}{ext}"
            new_path = IMAGES_DIR / new_filename
            shutil.move(str(old_path), str(new_path))
            news.cover_url = f"/images/{new_filename}"
            await db.flush()

    return news


async def mark_news_as_read(db: AsyncSession, news_id: int) -> bool:
    news = await get_news_by_id(db, news_id)
    if not news:
        return False
    news.is_read = 1
    if news.read_at is None:
        news.read_at = datetime.now()
    if news.is_later:
        news.is_later = 0
        news.later_at = None
    await db.flush()
    return True


async def get_news_stats(db: AsyncSession) -> dict:
    today_start = datetime.combine(date.today(), datetime.min.time())

    total_query = select(func.count()).where(News.is_deleted == 0)
    result = await db.execute(total_query)
    total = result.scalar()

    read_query = select(func.count()).where(News.is_deleted == 0, News.is_read == 1)
    result = await db.execute(read_query)
    read_count = result.scalar()

    unread_count = total - read_count

    today_query = select(func.count()).where(News.is_deleted == 0, News.collected_at >= today_start)
    result = await db.execute(today_query)
    today_count = result.scalar()

    category_query = (
        select(Category.name, func.count(News.id))
        .join(News, Category.id == News.category_id)
        .where(News.is_deleted == 0)
        .group_by(Category.name)
    )
    result = await db.execute(category_query)
    category_stats = {row[0]: row[1] for row in result.all()}

    range_start = datetime.combine(date.today() - timedelta(days=29), datetime.min.time())
    daily_query = (
        select(func.date(News.collected_at), Category.name, func.count(News.id))
        .outerjoin(Category, News.category_id == Category.id)
        .where(News.is_deleted == 0, News.collected_at >= range_start)
        .group_by(func.date(News.collected_at), Category.name)
    )
    result = await db.execute(daily_query)
    daily_map = {}
    for day_str, cat_name, count in result.all():
        if not day_str:
            continue
        name = cat_name or "默认"
        entry = daily_map.setdefault(day_str, {})
        entry[name] = entry.get(name, 0) + count

    daily_stats = []
    for i in range(29, -1, -1):
        day = date.today() - timedelta(days=i)
        day_str = day.isoformat()
        by_category = daily_map.get(day_str, {})
        daily_stats.append({
            "date": day_str,
            "count": sum(by_category.values()),
            "by_category": by_category,
        })

    read_query = (
        select(func.date(News.read_at), func.count(News.id))
        .where(News.is_deleted == 0, News.read_at.isnot(None))
        .group_by(func.date(News.read_at))
    )
    result = await db.execute(read_query)
    read_by_day = {row[0]: row[1] for row in result.all() if row[0]}

    recent_query = (
        select(News, Category.name, Category.color)
        .outerjoin(Category, News.category_id == Category.id)
        .where(News.is_deleted == 0, News.is_read == 1)
        .order_by(News.read_at.desc())
        .limit(10)
    )
    result = await db.execute(recent_query)
    recent_reads = [
        {
            "id": row.News.id,
            "title": row.News.title,
            "category_name": row[1],
            "category_color": row[2] or "#8a8690",
            "source": row.News.source,
            "collected_at": row.News.collected_at.isoformat() if row.News.collected_at else None,
            "read_at": row.News.read_at.isoformat() if row.News.read_at else None,
            "is_read": row.News.is_read,
        }
        for row in result.all()
    ]

    return {
        "total": total,
        "read_count": read_count,
        "unread_count": unread_count,
        "today_count": today_count,
        "category_stats": category_stats,
        "daily_stats": daily_stats,
        "read_by_day": read_by_day,
        "recent_reads": recent_reads,
    }


def _recommend_item(n: News) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "category_name": n.category_name,
        "category_color": n.category_color or "#8a8690",
        "source": n.source,
        "collected_at": n.collected_at.isoformat() if n.collected_at else None,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "is_read": n.is_read,
    }


async def _preferred_categories(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Category.name, func.count(News.id))
        .join(Category, News.category_id == Category.id)
        .where(News.is_deleted == 0, News.is_read == 1)
        .group_by(Category.name)
        .order_by(func.count(News.id).desc())
    )
    return [row[0] for row in result.all()]


async def _unread_by_categories(db: AsyncSession, cats: list[str], per: int) -> list[News]:
    items = []
    for cat in cats:
        result = await db.execute(
            select(News)
            .join(Category, News.category_id == Category.id)
            .where(News.is_deleted == 0, News.is_read == 0, Category.name == cat)
            .order_by(News.collected_at.desc())
            .limit(per)
        )
        items.extend(result.scalars().all())
    return items


async def get_recommended_news(db: AsyncSession, mode: str, limit: int = 10) -> dict:
    if mode == "autonomous":
        items, hint = await _recommend_autonomous(db, limit)
    else:
        items, hint = await _recommend_assist(db, limit)
    return {
        "mode": mode,
        "hint": hint,
        "items": [_recommend_item(n) for n in items],
    }


async def _recommend_assist(db: AsyncSession, limit: int) -> tuple[list[News], str]:
    today_start = datetime.combine(date.today(), datetime.min.time())
    result = await db.execute(
        select(Category.name, func.count(News.id))
        .join(Category, News.category_id == Category.id)
        .where(News.is_deleted == 0, News.is_read == 1, News.read_at >= today_start)
        .group_by(Category.name)
        .order_by(func.count(News.id).desc())
    )
    cats = [row[0] for row in result.all()]

    if len(cats) < 2:
        result = await db.execute(
            select(Category.name, func.count(News.id))
            .join(Category, News.category_id == Category.id)
            .where(News.is_deleted == 0, News.is_read == 0)
            .group_by(Category.name)
        )
        others = [row[0] for row in result.all() if row[0] not in cats and row[1] > 0]
        random.shuffle(others)
        cats = (cats + others)[:2]

    items = await _unread_by_categories(db, cats[:2], limit // 2)

    if len(items) < limit:
        have = {n.id for n in items}
        result = await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_read == 0)
            .order_by(News.collected_at.desc())
            .limit(limit)
        )
        for n in result.scalars().all():
            if n.id not in have:
                items.append(n)

    return items[:limit], "基于你今日阅读最多的分类挑选未读内容"


async def _recommend_autonomous(db: AsyncSession, limit: int) -> tuple[list[News], str]:
    result = await db.execute(
        select(News)
        .where(News.is_deleted == 0, News.is_read == 1, News.read_at.isnot(None))
        .order_by(News.read_at.desc())
        .limit(20)
    )
    read_news = result.scalars().all()

    score: dict[int, int] = {}
    for n in read_news:
        for rid in n.related_id_list:
            score[rid] = score.get(rid, 0) + 1

    items: list[News] = []
    if score:
        result = await db.execute(
            select(News).where(
                News.id.in_(list(score.keys())), News.is_deleted == 0, News.is_read == 0
            )
        )
        by_id = {n.id: n for n in result.scalars().all()}
        items = [by_id[i] for i in sorted(score, key=lambda i: -score[i]) if i in by_id]

    if len(items) < limit:
        pref = await _preferred_categories(db)
        have = {n.id for n in items}
        pools = []
        for cat in pref[:3]:
            result = await db.execute(
                select(News)
                .join(Category, News.category_id == Category.id)
                .where(News.is_deleted == 0, News.is_read == 0, Category.name == cat)
                .order_by(News.collected_at.desc())
                .limit(limit)
            )
            pools.append(list(result.scalars().all()))
        idx = 0
        while len(items) < limit and any(pools):
            pool = pools[idx % len(pools)]
            if pool:
                n = pool.pop(0)
                if n.id not in have:
                    items.append(n)
                    have.add(n.id)
            idx += 1

    if len(items) < limit:
        have = {n.id for n in items}
        result = await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_read == 0)
            .order_by(News.collected_at.desc())
            .limit(limit)
        )
        for n in result.scalars().all():
            if n.id not in have:
                items.append(n)

    return items[:limit], "AI 基于你的阅读偏好与智能关联分析推荐"


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
