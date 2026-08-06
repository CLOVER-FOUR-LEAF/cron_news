from datetime import datetime

from sqlalchemy import String, Text, DateTime, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    provider: Mapped[str] = mapped_column(String(50), nullable=False, comment="提供商ID(preset/custom)")
    provider_name: Mapped[str] = mapped_column(String(50), default="", comment="提供商显示名")
    name: Mapped[str] = mapped_column(String(100), default="", comment="显示名(模型名或提供商名)")
    base_url: Mapped[str] = mapped_column(String(300), default="", comment="Base URL")
    model_id: Mapped[str] = mapped_column(String(100), default="", comment="模型ID")
    config_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="类型(llm/image/search)")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否启用(每类型仅一个启用)")
    env_key: Mapped[str] = mapped_column(String(50), default="", comment="env中存放apikey的键名")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
