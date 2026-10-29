from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NewsTag(Base):
    """新闻标签的规范化索引表。

    news.tags 仍是展示用的 JSON 事实来源；本表用于精确过滤、分页、统计与排行，
    避免跨数据库用 JSON/LIKE 查询造成误匹配。写入新闻标签时必须同步维护。
    """

    __tablename__ = "news_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tag: Mapped[str] = mapped_column(String(50), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, comment="标签顺序，0 为主标签")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("news_id", "tag", name="uq_news_tags_news_tag"),
        Index("ix_news_tags_tag", "tag"),
        Index("ix_news_tags_news_id", "news_id"),
        Index("ix_news_tags_primary", "news_id", "position"),
    )
