"""标签与首页 API：标签总览、关注/取消关注、最新采集批次首页数据。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import recommend_service, retrieval, tag_service

router = APIRouter(prefix="/api", tags=["tags"])


class FollowModel(BaseModel):
    tag: str = Field(..., min_length=1, max_length=50)
    follow: bool = True


@router.get("/tags")
async def list_tags(
    batch_id: str | None = Query(None),
    limit: int = Query(40, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    overview = await tag_service.tag_overview(db, batch_id=batch_id)
    overview["items"] = overview["items"][:limit]
    return overview


@router.post("/tags/follow")
async def follow_tag(body: FollowModel, db: AsyncSession = Depends(get_db)):
    try:
        tag, follow = await tag_service.set_follow(db, body.tag, body.follow)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "tag": tag, "followed": follow}


@router.get("/home")
async def home_feed(
    limit: int = Query(60, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    batch_id, news_items = await tag_service.batch_news(db, limit=limit)
    followed = set(await tag_service.followed_tags(db))
    tags = tag_service.count_tags(news_items, limit=60)
    for item in tags:
        item["followed"] = item["tag"] in followed
    for tag in followed:
        if tag not in {item["tag"] for item in tags}:
            tags.append({"tag": tag, "count": 0, "color": tag_service.tag_color(tag), "followed": True})
    items = [retrieval.to_item(news, "batch") for news in news_items]
    recommendations = await recommend_service.personalized_recommendations(db, 10)
    return {
        "batch_id": batch_id,
        "batch_count": len(items),
        "items": items,
        "tags": tags,
        "followed": sorted(followed),
        "recommendations": recommendations.get("items") or [],
        "recommend_hint": recommendations.get("hint") or "",
    }
