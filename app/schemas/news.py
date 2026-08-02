from datetime import datetime
import json

from pydantic import BaseModel, Field, field_validator


class NewsBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="新闻标题")
    summary: str | None = Field(None, description="内容概要")
    content: str | None = Field(None, description="Markdown正文")
    source_url: str | None = Field(None, max_length=500, description="原文链接")
    source: str | None = Field(None, max_length=100, description="来源")
    category_id: int | None = Field(None, description="分类ID")
    cover_url: str | None = Field(None, max_length=500, description="封面图URL")
    collected_at: datetime | None = Field(None, description="收录时间")


class NewsCreate(NewsBase):
    pass


class NewsResponse(NewsBase):
    id: int
    category_name: str | None = Field(None, description="分类名称")
    category_color: str | None = Field(None, description="分类主题色")
    related_ids: list[int] = Field(default_factory=list, description="智能推荐相关新闻ID")
    is_read: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @field_validator("related_ids", mode="before")
    @classmethod
    def _parse_related_ids(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return [int(x) for x in parsed] if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError, TypeError):
                return []
        return v or []


class NewsListResponse(BaseModel):
    total: int
    items: list[NewsResponse]
