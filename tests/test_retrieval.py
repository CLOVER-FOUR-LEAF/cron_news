import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.database import get_session, init_db
from app.models import Category, News
from app.services import retrieval


async def _category_id(db, name: str) -> int:
    result = await db.execute(select(Category).where(Category.name == name))
    category = result.scalars().first()
    if category:
        return category.id
    category = Category(name=name, sort_order=50, color="#d4a853")
    db.add(category)
    await db.flush()
    return category.id


@pytest.mark.asyncio
async def test_fts_search_and_short_query_fallback():
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        category_id = await _category_id(db, "科技")
        db.add(
            News(
                title=f"英伟达发布新一代 AI 芯片 {token}",
                summary="面向推理场景，能效提升明显。",
                content="正文内容。",
                source="测试来源",
                source_url=f"https://example.com/{token}",
                category_id=category_id,
                collected_at=datetime.now(),
            )
        )
        await db.commit()

        long_query = await retrieval.search_news(db, f"英伟达 {token}", limit=5)
        # 未配置 embedding 时混合检索降级为词法，词法内部走 FTS
        assert long_query["sources"]["lexical"]["mode"] == "fts"
        assert any(token in item["title"] for item in long_query["items"])

        short_query = await retrieval.search_news(db, "芯片", limit=5)
        assert short_query["sources"]["lexical"]["mode"] == "like"
        assert short_query["items"]


@pytest.mark.asyncio
async def test_list_recent_respects_category():
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        category_id = await _category_id(db, "科技")
        db.add(
            News(
                title=f"最近新闻 {token}",
                summary="摘要",
                content="正文",
                category_id=category_id,
                collected_at=datetime.now(),
            )
        )
        await db.commit()

        result = await retrieval.list_recent(db, category="科技", days=1, limit=10)
        assert any(token in item["title"] for item in result["items"])
