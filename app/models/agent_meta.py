from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import quoted_name

from app.database import Base

# MariaDB 11.7+ / MySQL 9+ 将 VECTOR 视为保留字，列名必须显式加引号
VECTOR_COLUMN = quoted_name("vector", quote=True)


class NewsEmbedding(Base):
    """新闻向量。一条新闻一个文档级向量，只与同 model 的向量比较。

    `vector` 列是唯一事实来源（BLOB, float32, L2 归一化）；PostgreSQL 下另有
    `news_embedding_vec` 派生表作为 pgvector 索引，可随时重建。
    """

    __tablename__ = "news_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(
        VECTOR_COLUMN, LargeBinary, nullable=False, comment="float32 little-endian, L2 归一化"
    )
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("ix_news_embeddings_model", "model"),)


class LlmCall(Base):
    """模型调用计量，用于成本观察与后续限额。"""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), default="chat")
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("ix_llm_calls_conversation", "conversation_id"),
        Index("ix_llm_calls_created_at", "created_at"),
    )
