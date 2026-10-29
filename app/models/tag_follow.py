from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TagFollow(Base):
    """用户主动关注的标签。搜索 Agent 会优先围绕这些标签采集。"""

    __tablename__ = "tag_follows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tag: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (Index("ix_tag_follows_tag", "tag"),)
