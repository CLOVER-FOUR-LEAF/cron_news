"""辅助模式 API：外部 Agent 初始化、状态快照与协作状态。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import News
from app.services import assist_service, retrieval, tag_service

router = APIRouter(prefix="/api/assist", tags=["assist"])


class OnboardingPayload(BaseModel):
    version: int = Field(default=1, ge=1, le=99)
    agent: str = Field(default="", max_length=80, description="外部 Agent 名称")
    nickname: str = Field(default="", max_length=20, description="用户称呼（仅在默认昵称时写入）")
    sections: dict[str, str] = Field(
        default_factory=dict,
        description="记忆段落：profile/interests/watch/preferences/avoid/notes",
    )
    tags: list[str] | None = Field(
        default=None, max_length=20, description="要关注的标签；省略则保留上次状态"
    )
    schedule: dict[str, Any] | None = Field(
        default=None,
        description="外部 Agent 的采集节奏（平台只保存，不调度）；省略则保留上次状态",
    )
    editorial: dict[str, Any] | None = Field(
        default=None,
        description="内容口味：语言、深度、篇幅、来源偏好、简报风格、封面等；省略则保留上次状态",
    )
    replace_sections: bool = Field(
        default=False,
        description="true=覆盖对应记忆段落；false=追加并去重（默认，更安全）",
    )


@router.get("/status")
async def assist_status(request: Request, db: AsyncSession = Depends(get_db)):
    snapshot = await assist_service.status_snapshot(db)
    snapshot["base_url"] = str(request.base_url).rstrip("/")
    return snapshot


@router.get("/feed")
async def assist_feed(
    days: int = Query(7, ge=1, le=30, description="回看天数：1=近 24 小时，7=近一周"),
    limit: int = Query(300, ge=1, le=300, description="最多返回条数"),
    db: AsyncSession = Depends(get_db),
):
    """辅助模式资讯墙数据：按时间窗口返回新闻与标签统计，不依赖采集批次。"""
    since = datetime.now() - timedelta(days=days)
    base_query = select(News).where(News.is_deleted == 0, News.collected_at >= since)
    window_total = int(
        await db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    )
    rows = (
        await db.execute(base_query.order_by(News.collected_at.desc()).limit(limit))
    ).scalars().all()

    followed = await tag_service.followed_tags(db)
    followed_set = set(followed)
    tags = tag_service.count_tags(rows, limit=60)
    for item in tags:
        item["followed"] = item["tag"] in followed_set
    known = {item["tag"] for item in tags}
    for tag in followed:
        if tag not in known:
            tags.append(
                {
                    "tag": tag,
                    "count": 0,
                    "color": tag_service.tag_color(tag),
                    "followed": True,
                }
            )
    tags.sort(key=lambda item: (not item.get("followed"), -(item.get("count") or 0), item["tag"]))

    batch_id = await tag_service.latest_batch_id(db)
    batch_count = 0
    if batch_id:
        batch_count = int(
            await db.scalar(
                select(func.count()).where(
                    News.is_deleted == 0, News.batch_id == batch_id
                )
            )
            or 0
        )

    return {
        "window_days": days,
        "total": window_total,
        "returned": len(rows),
        "truncated": window_total > len(rows),
        "items": [retrieval.to_item(news, "window") for news in rows],
        "tags": tags,
        "followed": followed,
        "batch_id": batch_id,
        "batch_count": batch_count,
    }


@router.post("/onboarding")
async def assist_onboarding(
    payload: OnboardingPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not assist_service.is_assist():
        raise HTTPException(
            status_code=409,
            detail=(
                "当前不是辅助模式，外部 Agent 初始化已拒绝。"
                "请先在平台「设置 → 工作模式」切换为辅助模式；"
                "切换不会丢失新闻、记忆与图片。"
            ),
        )
    total_chars = sum(len(value or "") for value in payload.sections.values())
    if total_chars > 30000:
        raise HTTPException(status_code=400, detail="初始化记忆内容过长（上限 30000 字）")
    invalid_sections = [
        key for key in payload.sections if key not in assist_service.ONBOARDING_SECTIONS
    ]
    if invalid_sections:
        raise HTTPException(
            status_code=400,
            detail=(
                "不支持的记忆段落："
                + "、".join(invalid_sections)
                + "；可用："
                + "、".join(assist_service.ONBOARDING_SECTIONS)
            ),
        )
    if (
        not payload.sections
        and not payload.tags
        and payload.schedule is None
        and payload.editorial is None
        and not payload.nickname
    ):
        raise HTTPException(
            status_code=400,
            detail="初始化内容为空；至少提供 sections / tags / schedule / editorial / nickname 之一",
        )
    meta_chars = len(json.dumps(payload.schedule or {}, ensure_ascii=False)) + len(
        json.dumps(payload.editorial or {}, ensure_ascii=False)
    )
    if meta_chars > 20000:
        raise HTTPException(status_code=400, detail="schedule / editorial 内容过长（合计上限 20000 字）")

    try:
        result = await assist_service.apply_onboarding(
            db,
            sections=payload.sections,
            tags=payload.tags,
            nickname=payload.nickname,
            agent=payload.agent,
            schedule=payload.schedule,
            editorial=payload.editorial,
            replace_sections=payload.replace_sections,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    result["base_url"] = str(request.base_url).rstrip("/")
    return result


@router.delete("/onboarding")
async def reset_assist_onboarding():
    """清除外部 Agent 的初始化状态（ASSIST.json），不删除记忆与关注标签。"""
    if not assist_service.is_assist():
        raise HTTPException(status_code=409, detail="当前不是辅助模式")
    try:
        return assist_service.reset_onboarding_state()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
