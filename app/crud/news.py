from datetime import datetime, date, timedelta
import os
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

    daily_stats = []
    for i in range(7, 0, -1):
        day = date.today() - timedelta(days=i)
        day_start = datetime.combine(day, datetime.min.time())
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time())
        day_query = select(func.count()).where(
            News.is_deleted == 0,
            News.collected_at >= day_start,
            News.collected_at < day_end,
        )
        result = await db.execute(day_query)
        count = result.scalar()
        daily_stats.append({
            "date": day.strftime("%m-%d"),
            "count": count,
        })

    recent_query = (
        select(News, Category.name, Category.color)
        .outerjoin(Category, News.category_id == Category.id)
        .where(News.is_deleted == 0, News.is_read == 1)
        .order_by(News.collected_at.desc())
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
        "recent_reads": recent_reads,
    }
