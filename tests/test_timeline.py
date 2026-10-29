"""时间范围解析与时间线检索。"""

import uuid
from datetime import datetime, timedelta

import pytest

from app.database import get_session, init_db
from app.models import News
from app.services import retrieval, tag_service, time_range


def test_parse_chinese_time_ranges():
    now = datetime(2026, 10, 29, 12, 0, 0)
    half_month = time_range.parse_time_range("过去半个月", now=now)
    assert half_month["days"] == 15
    assert half_month["until"] == now
    two_weeks = time_range.parse_time_range("最近两周", now=now)
    assert two_weeks["days"] == 14
    last_month = time_range.parse_time_range("上个月", now=now)
    assert last_month["since"] == datetime(2026, 9, 1)
    assert last_month["until"] == datetime(2026, 10, 1)
    explicit = time_range.parse_time_range("2026-09-01 到 2026-09-15", now=now)
    assert explicit["since"] == datetime(2026, 9, 1)
    assert explicit["until"] == datetime(2026, 9, 16)


@pytest.mark.asyncio
async def test_timeline_covers_multiple_buckets():
    await init_db()
    token = uuid.uuid4().hex[:8]
    now = datetime.now()
    async with get_session() as db:
        for offset, title in [
            (14, f"AI 芯片架构发布 {token}"),
            (7, f"AI 芯片监管新规 {token}"),
            (1, f"AI 芯片出货数据 {token}"),
        ]:
            news = News(
                title=title,
                summary="AI 行业发展",
                content="正文",
                source="测试来源",
                source_url=f"https://example.com/{token}-{offset}",
                collected_at=now - timedelta(days=offset),
                tags='["AI芯片"]',
            )
            db.add(news)
            await db.flush()
            await tag_service.sync_news_tags(db, news)
        await db.commit()

    async with get_session() as db:
        result = await retrieval.search_timeline(
            db,
            "AI",
            since=now - timedelta(days=15),
            until=now,
            bucket_days=5,
            per_bucket=6,
            max_events=30,
        )
    assert len(result["timeline"]) >= 3
    dates = [item["date"] for item in result["timeline"]]
    assert dates == sorted(dates)
    assert result["coverage"]["days_with_news"] >= 3
    assert any(bucket["count"] > 0 for bucket in result["buckets"])
