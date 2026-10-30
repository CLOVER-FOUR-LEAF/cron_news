from datetime import datetime
import json

from pydantic import BaseModel, Field, field_validator


def _clean_url(value: str | None, *, allow_local_images: bool = False) -> str | None:
    """URL 字段白名单：http(s) 或（封面）本地 /images/ 路径。

    这些字段会进入模板的 href/src，必须在入口拒绝引号、控制字符与 javascript: 等协议，
    避免存储型 XSS；同时保留站内封面的相对路径能力。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if any(ord(char) < 32 for char in text) or any(char in text for char in ('"', "'", "<", ">")):
        raise ValueError("URL 包含非法字符")
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return text
    if allow_local_images and text.startswith("/images/"):
        if ".." in text:
            raise ValueError("URL 不允许包含 ..")
        return text
    hint = "、https:// 或 /images/ 本地路径" if allow_local_images else " 或 https://"
    raise ValueError(f"URL 必须以 http://{hint} 开头")


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
    batch_id: str | None = Field(
        None,
        max_length=40,
        description="采集批次ID；同一轮推送的所有新闻使用相同值，首页据此展示「上一批」",
    )

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value):
        return _clean_url(value)

    @field_validator("cover_url")
    @classmethod
    def _validate_cover_url(cls, value):
        return _clean_url(value, allow_local_images=True)


class NewsUpdate(BaseModel):
    """外部 Agent 更正已推送新闻时使用的增量字段；未提供的字段保持不变。"""

    title: str | None = Field(None, min_length=1, max_length=255)
    summary: str | None = None
    content: str | None = None
    source_url: str | None = Field(None, max_length=500)
    source: str | None = Field(None, max_length=100)
    cover_url: str | None = Field(None, max_length=500)
    tags: list[str] | None = None
    credibility: int | None = Field(None, ge=0, le=100)
    analysis: dict | None = None
    related_ids: list[int] | None = None

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value):
        return _clean_url(value)

    @field_validator("cover_url")
    @classmethod
    def _validate_cover_url(cls, value):
        return _clean_url(value, allow_local_images=True)


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
