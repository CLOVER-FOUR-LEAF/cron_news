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
    env_key: Mapped[str] = mapped_column(String(50), default="", comment="旧版env键名(迁移用)")
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密后的API Key")
    native_search: Mapped[bool] = mapped_column(Boolean, default=False, comment="大模型是否具备可用内置搜索")
    native_search_kind: Mapped[str] = mapped_column(String(30), default="", comment="内置搜索适配器(deepseek/kimi/zhipu/mimo)")
    native_search_detail: Mapped[str] = mapped_column(String(300), default="", comment="内置搜索检测说明")
    native_search_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近检测时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
