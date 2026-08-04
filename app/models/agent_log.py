from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="运行开始时间")
    status: Mapped[str] = mapped_column(String(20), default="finished", comment="运行状态")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="日志行JSON")
