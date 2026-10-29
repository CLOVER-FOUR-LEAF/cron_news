from datetime import datetime
import json

from pydantic import BaseModel, Field, field_validator


class NewsBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255, description="新闻标题")
    summary: str | None = Field(None, description="内容概要（建议 100-300 字）")
    content: str | None = Field(None, description="Markdown正文")
    source_url: str | None = Field(None, max_length=500, description="原文链接")
    source: str | None = Field(None, max_length=100, description="来源")
    cover_url: str | None = Field(None, max_length=500, description="封面图URL")
    collected_at: datetime | None = Field(None, description="收录时间")
    tags: list[str] | None = Field(None, description="主题标签（一条新闻可有多个）")
    credibility: int | None = Field(None, ge=0, le=100, description="来源可信度 0-100（可选）")
    analysis: dict | None = Field(None, description="结构化分析（可选：key_points/topics/why_it_matters/entities）")


class NewsCreate(NewsBase):
    pass


class NewsResponse(NewsBase):
    id: int
    batch_id: str | None = Field(None, description="采集批次ID")
    short_summary: str = Field("", description="适合卡片展示的短摘要")
    tags: list[str] = Field(default_factory=list, description="主题标签")
    credibility: int | None = Field(None, description="来源可信度评分(0-100)")
    analysis: dict = Field(default_factory=dict, description="结构化分析(要点/实体/信源判断)")
    related_ids: list[int] = Field(default_factory=list, description="智能推荐相关新闻ID")
    is_read: int = 0
    is_reading: int = 0
    is_fav: int = 0
    fav_at: datetime | None = None
    is_later: int = 0
    later_at: datetime | None = None
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

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return [str(x) for x in parsed] if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError, TypeError):
                return []
        return v or []

    @field_validator("analysis", mode="before")
    @classmethod
    def _parse_analysis(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                return {}
        return v or {}


class NewsListResponse(BaseModel):
    total: int
    items: list[NewsResponse]
