"""长期记忆、画像与反馈闭环。"""

import uuid
from datetime import datetime

import pytest

from app.database import get_session, init_db
from app.models import Feedback, News
from app.services import feedback_service, memory_service, recommend_service, tag_service, user_profile


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_service, "memory_dir", lambda: tmp_path)
    monkeypatch.setenv("AGENT_MEMORY_ENABLED", "true")
    return tmp_path


@pytest.mark.asyncio
async def test_remember_and_forget(memory_dir):
    await memory_service.remember("跟踪 AI 芯片与大模型的进展（科技）", "watch")
    await memory_service.remember("回答先给结论，再给要点", "preferences")

    text = await memory_service.read_memory()
    assert "AI 芯片" in memory_service.read_section(text, "watch")
    assert "先给结论" in memory_service.read_section(text, "preferences")

    removed = await memory_service.forget("AI 芯片")
    assert removed == 1
    text = await memory_service.read_memory()
    assert "AI 芯片" not in memory_service.read_section(text, "watch")


@pytest.mark.asyncio
async def test_write_memory_preserves_structure(memory_dir):
    content = await memory_service.write_memory("# 我的记忆\n\n## 兴趣与关注\n- 地缘政治\n")
    assert "## 用户画像" in content
    assert "## 控制任务" not in content
    assert "## 跟踪任务" in content
    assert memory_service.AUTO_START in content
    assert "地缘政治" in memory_service.read_section(content, "interests")


@pytest.mark.asyncio
async def test_extract_terms_for_recommendations(memory_dir):
    await memory_service.remember("跟踪 AI 芯片（科技）", "watch")
    await memory_service.remember("少推娱乐八卦", "avoid")
    text = await memory_service.read_memory()
    interests = memory_service.extract_terms(text, ("watch", "interests"))
    avoids = memory_service.extract_terms(text, ("avoid",))
    assert "AI" in interests
    assert "芯片" in interests
    assert "娱乐八卦" in avoids


@pytest.mark.asyncio
async def test_compute_profile_uses_behavior_and_feedback(memory_dir):
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        read_news = News(
            title=f"AI 芯片深度分析 {token}",
            summary="测试",
            content="正文",
            source="测试来源",
            is_read=1,
            is_fav=1,
            reading_at=datetime.now(),
            read_at=datetime.now(),
            collected_at=datetime.now(),
            tags='["AI芯片", "半导体"]',
        )
        disliked = News(
            title=f"无聊的广告 {token}",
            summary="测试",
            source="低质来源",
            collected_at=datetime.now(),
            tags='["广告"]',
        )
        db.add_all([read_news, disliked])
        await db.flush()
        await tag_service.sync_news_tags(db, read_news)
        await tag_service.sync_news_tags(db, disliked)
        db.add(
            Feedback(
                target_type="news",
                target_id=disliked.id,
                rating=-1,
                reason="irrelevant",
                comment="不是我想看的",
                status="noted",
            )
        )
        await db.commit()
        disliked_id = disliked.id

    async with get_session() as db:
        profile = await user_profile.compute_profile(db)

    tags = {item["tag"]: item for item in profile["tags"]}
    assert tags["AI芯片"]["read"] >= 1
    assert tags["AI芯片"]["fav"] >= 1
    topics = {item["tag"] for item in profile["topics"]}
    assert "AI芯片" in topics
    assert disliked_id in profile["feedback"]["disliked_news_ids"]
    assert profile["has_signal"] is True


@pytest.mark.asyncio
async def test_feedback_writes_memory_and_reason(memory_dir):
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        news = News(
            title=f"浅层快讯 {token}",
            summary="只有一句摘要",
            source="测试来源",
            collected_at=datetime.now(),
        )
        db.add(news)
        await db.flush()
        feedback = await feedback_service.create_feedback(
            db,
            target_type="news",
            target_id=news.id,
            rating=-1,
            reason="shallow",
            comment="希望看原文深度报道",
        )
        await db.commit()
        assert feedback.status == "noted"

    text = await memory_service.read_memory()
    feedback_section = memory_service.read_section(text, "feedback")
    assert "内容太浅" in feedback_section
    assert "原文" in feedback_section
    avoid = memory_service.read_section(text, "avoid")
    assert "浅层快讯" in avoid or "摘要" in avoid


@pytest.mark.asyncio
async def test_personalized_recommendations_follow_reading_profile(memory_dir):
    await init_db()
    token = uuid.uuid4().hex[:8]
    async with get_session() as db:
        read_news = News(
            title=f"已读的 AI 芯片深度报道 {token}",
            summary="测试",
            source="测试来源",
            is_read=1,
            is_fav=1,
            read_at=datetime.now(),
            collected_at=datetime.now(),
            tags='["AI芯片"]',
        )
        candidate = News(
            title=f"未读的 AI 芯片新进展 {token}",
            summary="测试",
            source="测试来源",
            collected_at=datetime.now(),
            tags='["AI芯片"]',
        )
        db.add_all([read_news, candidate])
        await db.flush()
        await tag_service.sync_news_tags(db, read_news)
        await tag_service.sync_news_tags(db, candidate)
        await db.commit()
        candidate_id = candidate.id

    async with get_session() as db:
        result = await recommend_service.personalized_recommendations(db, 30)

    items = result["items"]
    assert items
    assert all(item["is_read"] == 0 for item in items)
    matched = [item for item in items if item["id"] == candidate_id]
    assert matched and matched[0].get("reason")


def test_render_auto_profile_contains_key_signals():
    profile = {
        "generated_at": "2026-10-28T12:00:00",
        "totals": {"news": 20, "read": 8, "fav": 2, "read_week": 5, "read_rate": 0.4, "avg_dwell_seconds": 42},
        "tags": [{"tag": "AI芯片", "read": 6, "fav": 2, "affinity": 0.9}],
        "topics": [{"tag": "AI芯片", "count": 4}],
        "sources": [{"name": "路透社", "read": 3, "affinity": 0.8}],
        "recent_reads": [{"id": 1, "title": "测试新闻", "tags": ["AI芯片"]}],
        "feedback": {"negative_reasons": {"shallow": 2}, "positive_reasons": {"helpful": 1}},
        "reading_hours": [{"hour": 9, "count": 3}],
    }
    block = user_profile.render_auto_profile(profile)
    assert "自动生成于 2026-10-28T12:00:00" in block
    assert "AI芯片" in block
    assert "内容太浅" in block
