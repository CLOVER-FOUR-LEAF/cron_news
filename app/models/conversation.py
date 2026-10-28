from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="长对话摘要")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    is_deleted: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_conversations_updated_at", "updated_at"),
        Index("ix_conversations_is_deleted", "is_deleted"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, comment="user/assistant/tool/system")
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具调用轨迹 JSON")
    tool_call_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True, comment="引用来源 JSON")
    usage: Mapped[str | None] = mapped_column(Text, nullable=True, comment="token 用量 JSON")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_messages_conversation_id_id", "conversation_id", "id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
