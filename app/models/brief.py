from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    category_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="分类名称")
    brief_date: Mapped[str] = mapped_column(String(10), nullable=False, comment="简报日期(YYYY-MM-DD)")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="简报Markdown内容")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="生成时间")


class BriefNote(Base):
    __tablename__ = "brief_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    category_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="分类名称")
    brief_date: Mapped[str] = mapped_column(String(10), nullable=False, comment="简报日期(YYYY-MM-DD)")
    content: Mapped[str] = mapped_column(Text, default="", comment="便签内容")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
