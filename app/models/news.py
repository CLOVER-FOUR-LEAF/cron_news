from datetime import datetime
import json

from sqlalchemy import String, Text, DateTime, SmallInteger, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class News(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="新闻标题")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="内容概要")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Markdown正文")
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原文链接")
    source: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="来源")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="封面图URL")
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="收录时间")
    batch_id: Mapped[str | None] = mapped_column(String(40), nullable=True, comment="采集批次ID")
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
    # 采集质量：主题标签、来源可信度、结构化分析（原文抓取/要点/实体/信源判断）
    tags: Mapped[str | None] = mapped_column(Text, nullable=True, comment="主题标签(JSON数组)")
    credibility: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="来源可信度评分(0-100)")
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True, comment="结构化分析(JSON)")

    @property
    def related_id_list(self) -> list[int]:
        if not self.related_ids:
            return []
        try:
            parsed = json.loads(self.related_ids)
            return [int(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    @property
    def tag_list(self) -> list[str]:
        if not self.tags:
            return []
        try:
            parsed = json.loads(self.tags)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError, TypeError):
            return []

    @property
    def analysis_data(self) -> dict:
        if not self.analysis:
            return {}
        try:
            parsed = json.loads(self.analysis)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}

    @property
    def short_summary(self) -> str:
        from app.services.text_utils import short_summary

        return short_summary(self.summary, self.content)
