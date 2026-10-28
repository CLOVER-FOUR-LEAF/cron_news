"""会话与消息持久化。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message


async def create_conversation(db: AsyncSession, title: str = "新对话") -> Conversation:
    conversation = Conversation(title=(title or "新对话").strip()[:200] or "新对话")
    db.add(conversation)
    await db.flush()
    await db.refresh(conversation)
    return conversation


async def get_conversation(db: AsyncSession, conversation_id: int) -> Conversation | None:
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.is_deleted == 0)
    )
    return result.scalars().first()


async def list_conversations(db: AsyncSession, limit: int = 30, offset: int = 0) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.is_deleted == 0)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 100)))
    )
    return list(result.scalars().all())


async def update_conversation(
    db: AsyncSession,
    conversation_id: int,
    *,
    title: str | None = None,
    summary: str | None = None,
) -> Conversation | None:
    conversation = await get_conversation(db, conversation_id)
    if not conversation:
        return None
    if title is not None and title.strip():
        conversation.title = title.strip()[:200]
    if summary is not None:
        conversation.summary = summary
    conversation.updated_at = datetime.now()
    await db.flush()
    return conversation


async def soft_delete_conversation(db: AsyncSession, conversation_id: int) -> bool:
    conversation = await get_conversation(db, conversation_id)
    if not conversation:
        return False
    conversation.is_deleted = 1
    await db.flush()
    return True


async def add_message(
    db: AsyncSession,
    conversation_id: int,
    role: str,
    content: str,
    *,
    tool_calls: str | None = None,
    tool_call_id: str | None = None,
    name: str | None = None,
    citations: str | None = None,
    usage: str | None = None,
    status: str = "ok",
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content or "",
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        name=name,
        citations=citations,
        usage=usage,
        status=status,
    )
    db.add(message)

    conversation = await get_conversation(db, conversation_id)
    if conversation:
        conversation.message_count = (conversation.message_count or 0) + 1
        conversation.updated_at = datetime.now()

    await db.flush()
    await db.refresh(message)
    return message


async def get_messages(
    db: AsyncSession,
    conversation_id: int,
    limit: int = 50,
    before_id: int | None = None,
) -> list[Message]:
    query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.role.in_(["user", "assistant"]),
    )
    if before_id:
        query = query.where(Message.id < before_id)
    query = query.order_by(Message.id.desc()).limit(max(1, min(limit, 200)))
    result = await db.execute(query)
    return list(reversed(result.scalars().all()))


async def load_history(
    db: AsyncSession,
    conversation_id: int,
    limit: int = 12,
    before_id: int | None = None,
) -> list[dict[str, str]]:
    messages = await get_messages(db, conversation_id, limit, before_id)
    return [
        {"role": message.role, "content": message.content or ""}
        for message in messages
        if (message.content or "").strip() and message.status == "ok"
    ]
