from datetime import datetime
import json

from sqlalchemy import String, Text, DateTime, SmallInteger, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class News(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="新闻标题")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="内容概要")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Markdown正文")
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原文链接")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="来源")
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True, comment="分类ID")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="封面图URL")
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="收录时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
    is_deleted: Mapped[int] = mapped_column(SmallInteger, default=0, comment="逻辑删除")
    is_read: Mapped[int] = mapped_column(SmallInteger, default=0, comment="已读状态")
    is_reading: Mapped[int] = mapped_column(SmallInteger, default=0, comment="在读状态")
    reading_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始阅读时间")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="阅读时间")
    is_fav: Mapped[int] = mapped_column(SmallInteger, default=0, comment="收藏状态")
    fav_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="收藏时间")
    is_later: Mapped[int] = mapped_column(SmallInteger, default=0, comment="稍后再读状态")
    later_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="稍后再读标记时间")
    related_ids: Mapped[str | None] = mapped_column(Text, nullable=True, comment="智能推荐相关新闻ID集合(JSON)")

    category = relationship("Category", backref="news_items", lazy="joined")

    @property
    def category_name(self) -> str | None:
        return self.category.name if self.category else None

    @property
    def category_color(self) -> str | None:
        return self.category.color if self.category else None

    @property
    def related_id_list(self) -> list[int]:
        if not self.related_ids:
            return []
        try:
            parsed = json.loads(self.related_ids)
            return [int(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []
