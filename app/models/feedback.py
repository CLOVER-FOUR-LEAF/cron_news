from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Feedback(Base):
    """用户反馈：对话回答、新闻文章、推荐结果。

    反馈既用于即时的偏好惩罚（推荐打分），也会被 Agent 反思后写入长期记忆。
    status: new（刚记录）/ noted（已写入确定性记忆）/ processed（已完成 LLM 反思）/ failed（多次失败）
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(
        String(20), default="news", comment="news/message/recommend/chat"
    )
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="新闻ID/消息ID")
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[int] = mapped_column(SmallInteger, default=0, comment="1=满意 / 0=中性 / -1=不满意")
    reason: Mapped[str] = mapped_column(String(40), default="", comment="原因代码")
    comment: Mapped[str] = mapped_column(Text, default="", comment="用户补充说明")
    excerpt: Mapped[str] = mapped_column(Text, default="", comment="被反馈对象的摘要快照")
    status: Mapped[str] = mapped_column(String(20), default="new")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_feedback_created_at", "created_at"),
        Index("ix_feedback_status", "status"),
        Index("ix_feedback_target", "target_type", "target_id"),
    )
