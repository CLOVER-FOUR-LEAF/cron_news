"""回归测试：并发生成正文/封面时不能共用同一个 AsyncSession。

历史缺陷：封面任务共用主 session，任一条失败 rollback 后其它 ORM 对象被 expire，
随后访问属性触发 MissingGreenlet，整轮采集失败。
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.database import get_session, init_db
from app.models import Category, News
from app.services import ai_service


async def _category(db, name: str) -> int:
    row = (await db.execute(select(Category).where(Category.name == name))).scalars().first()
    if row:
        return row.id
    row = Category(name=name, sort_order=60, color="#d4a853")
    db.add(row)
    await db.flush()
    return row.id


@pytest.mark.asyncio
async def test_process_search_results_many_items_with_covers(monkeypatch):
    """多条候选 + 封面开启时，整轮应成功入库且每条都拿到 cover_url。"""
    await init_db()
    token = uuid.uuid4().hex[:8]

    # 避免真实模型调用：正文与封面都走本地桩
    async def fake_content(db, title, summary):
        return f"正文：{title}"

    async def fake_cover(db, news, emit=None):
        # 模拟较慢的封面生成，放大并发窗口
        import asyncio

        await asyncio.sleep(0.01)
        news.cover_url = f"/images/cover/{news.id}.svg"
        await db.flush()
        return news.cover_url

    monkeypatch.setattr(ai_service, "generate_news_content", fake_content)
    monkeypatch.setattr(ai_service, "assign_news_cover", fake_cover)
    monkeypatch.setattr(ai_service, "cover_mode", lambda: "svg")

    results = [
        {"title": f"测试并发新闻 {token}-{i}", "snippet": "摘要", "url": f"https://e/{i}"}
        for i in range(6)
    ]

    async with get_session() as db:
        await _category(db, "科技")
        await db.commit()

    async with get_session() as db:
        count = await ai_service.process_search_results(db, "科技", results)
        await db.commit()
    assert count == 6

    async with get_session() as db:
        rows = (
            await db.execute(select(News).where(News.title.contains(token)))
        ).scalars().all()
    assert len(rows) == 6
    assert all(row.cover_url for row in rows), "每条新闻都应写入封面"
