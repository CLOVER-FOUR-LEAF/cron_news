"""用户反馈：记录、确定性记忆、后台反思调度。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import Feedback, Message, News
from app.services import memory_service

logger = get_logger("feedback")

TARGET_TYPES = {"news", "message", "recommend", "chat"}
POSITIVE_REASONS = {"helpful"}
_reason_labels = memory_service.REASON_LABELS
_background_tasks: set[asyncio.Task] = set()


def _env_bool(name: str, default: bool) -> bool:
    raw = (read_env_file().get(name, "") or os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _clip(text: str | None, limit: int = 300) -> str:
    return (text or "").strip()[:limit]


async def _resolve_target(
    db: AsyncSession, target_type: str, target_id: int | None
) -> tuple[str, dict[str, Any]]:
    if target_type == "news" and target_id:
        news = (await db.execute(select(News).where(News.id == target_id))).scalars().first()
        if not news:
            raise ValueError("新闻不存在")
        excerpt = f"{news.title}｜{(news.summary or '')[:120]}"
        return excerpt, {
            "title": news.title,
            "tags": news.tag_list,
            "source": news.source,
        }
    if target_type == "message" and target_id:
        message = (await db.execute(select(Message).where(Message.id == target_id))).scalars().first()
        if not message:
            raise ValueError("消息不存在")
        return f"助手回答：{(message.content or '')[:160]}", {"excerpt": message.content or ""}
    return "", {}


async def create_feedback(
    db: AsyncSession,
    *,
    target_type: str = "news",
    target_id: int | None = None,
    conversation_id: int | None = None,
    rating: int = -1,
    reason: str = "",
    comment: str = "",
) -> Feedback:
    target_type = (target_type or "news").strip().lower()
    if target_type not in TARGET_TYPES:
        raise ValueError("不支持的反馈对象")
    rating = 1 if rating > 0 else (-1 if rating < 0 else 0)
    reason = _clip(reason, 40)
    comment = _clip(comment, 800)

    excerpt, detail = await _resolve_target(db, target_type, target_id)
    if target_type == "message" and target_id and conversation_id is None:
        message = (await db.execute(select(Message).where(Message.id == target_id))).scalars().first()
        conversation_id = message.conversation_id if message else None

    feedback = Feedback(
        target_type=target_type,
        target_id=target_id,
        conversation_id=conversation_id,
        rating=rating,
        reason=reason,
        comment=comment,
        excerpt=excerpt,
        status="new",
    )
    db.add(feedback)
    await db.flush()

    try:
        line = await memory_service.record_feedback(feedback, detail)
        if line:
            feedback.status = "noted"
            feedback.attempts = 1
            logger.info("反馈已写入记忆：%s", line)
    except Exception as exc:
        logger.warning("反馈写入确定性记忆失败（仍保留反馈记录）：%s", exc)

    await db.flush()
    return feedback


def schedule_reflection() -> None:
    """反馈后异步触发记忆反思；不阻塞用户请求。"""
    if not memory_service.enabled() or not _env_bool("AGENT_FEEDBACK_REFLECT", True):
        return
    task = asyncio.create_task(memory_service.reflect_pending_feedback_background())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def list_feedback(
    db: AsyncSession,
    *,
    limit: int = 20,
    status: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    query = select(Feedback).order_by(Feedback.id.desc()).limit(limit)
    if status:
        query = query.where(Feedback.status == status)
    rows = (await db.execute(query)).scalars().all()

    news_ids = {item.target_id for item in rows if item.target_type == "news" and item.target_id}
    titles: dict[int, str] = {}
    if news_ids:
        for news in (await db.execute(select(News).where(News.id.in_(news_ids)))).scalars().all():
            titles[news.id] = news.title

    items = []
    for item in rows:
        items.append(
            {
                "id": item.id,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "conversation_id": item.conversation_id,
                "rating": item.rating,
                "reason": item.reason,
                "reason_label": _reason_labels.get(item.reason, item.reason or "未说明"),
                "comment": item.comment,
                "status": item.status,
                "title": titles.get(item.target_id or 0) or "",
                "excerpt": item.excerpt,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "processed_at": item.processed_at.isoformat() if item.processed_at else None,
            }
        )
    return {
        "items": items,
        "total": len(items),
        "pending": sum(1 for item in rows if item.status in {"new", "noted"}),
        "labels": _reason_labels,
    }


def reasons() -> list[dict[str, str]]:
    return [
        {"value": key, "label": label, "remedy": memory_service.REMEDIES.get(key, "")}
        for key, label in _reason_labels.items()
    ]
