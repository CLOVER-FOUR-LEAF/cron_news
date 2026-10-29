"""标签归一化、关注与聚合。"""

import uuid
from datetime import datetime

import pytest

from app.database import get_session, init_db
from app.models import News
from app.services import tag_service


def test_normalize_tags_filters_generic_and_dedupes():
    tags = tag_service.normalize_tags(["#AI芯片", "ai芯片", "新闻", "资讯", "半导体", ""])
    assert tags == ["AI芯片", "半导体"]


@pytest.mark.asyncio
async def test_follow_and_top_tags():
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        tag = f"测试标签{token}"
        await tag_service.set_follow(db, tag, True)
        news = News(
            title=f"标签测试 {token}",
            summary="摘要",
            content="正文",
            collected_at=datetime.now(),
            tags=f'["{tag}", "AI芯片"]',
        )
        db.add(news)
        await db.flush()
        await tag_service.sync_news_tags(db, news)
        await db.commit()

    async with get_session() as db:
        followed = await tag_service.followed_tags(db)
        assert tag in followed
        tags = await tag_service.top_tags(db, limit=50)
        by_name = {item["tag"]: item for item in tags}
        assert by_name[tag]["count"] >= 1
        items, total = await tag_service.tag_news(db, tag)
        assert total >= 1
        assert any(news.id == items[0].id for news in items)
        await tag_service.set_follow(db, tag, False)
        await db.commit()
        assert tag not in await tag_service.followed_tags(db)


def test_app_exposes_feedback_memory_and_tag_routes():
    from app.main import app

    paths = set(app.openapi().get("paths", {}))
    assert "/api/feedback" in paths
    assert "/api/memory" in paths
    assert "/api/tags" in paths
    assert "/api/home" in paths
    assert "/api/retrieval/timeline" in paths
