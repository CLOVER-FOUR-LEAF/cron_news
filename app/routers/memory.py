"""记忆与反馈 API：对话页右侧面板、文章页反馈、外部 Agent 读取画像。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logging_setup import get_logger
from app.services import feedback_service, memory_service, user_profile

logger = get_logger("memory_api")

router = APIRouter(prefix="/api", tags=["memory"])


class MemoryUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)


class FeedbackCreate(BaseModel):
    target_type: str = Field(default="news", max_length=20)
    target_id: int | None = None
    conversation_id: int | None = None
    rating: int = Field(default=-1, ge=-1, le=1)
    reason: str = Field(default="", max_length=40)
    comment: str = Field(default="", max_length=800)


@router.get("/memory")
async def get_memory(db: AsyncSession = Depends(get_db)):
    if not memory_service.enabled():
        return {"enabled": False, "content": "", "sections": {}, "profile": {}, "feedback": []}
    try:
        profile = await memory_service.ensure_profile_fresh(db)
        if not profile:
            profile = await user_profile.compute_profile(db)
    except Exception as exc:
        logger.warning("读取用户画像失败：%s", exc)
        profile = {}
    content = await memory_service.read_memory()
    feedback = await feedback_service.list_feedback(db, limit=12)
    return {
        "enabled": True,
        "content": content,
        "sections": memory_service.sections(content),
        "profile": profile,
        "feedback": feedback.get("items") or [],
        "feedback_pending": feedback.get("pending") or 0,
        "meta": memory_service.status(),
    }


@router.put("/memory")
async def save_memory(body: MemoryUpdate):
    try:
        content = await memory_service.write_memory(body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "content": content, "sections": memory_service.sections(content)}


@router.post("/memory/refresh")
async def refresh_memory(db: AsyncSession = Depends(get_db)):
    """强制重建行为画像，并让 Agent 反思尚未消化的反馈。"""
    try:
        profile = await memory_service.ensure_profile_fresh(db, force=True)
        if not profile:
            profile = await user_profile.compute_profile(db)
        reflection = await memory_service.reflect_pending_feedback(db)
        await db.commit()
    except Exception as exc:
        logger.exception("刷新记忆失败：%s", exc)
        raise HTTPException(status_code=500, detail=f"刷新记忆失败：{exc}")
    content = await memory_service.read_memory()
    feedback = await feedback_service.list_feedback(db, limit=12)
    return {
        "ok": True,
        "content": content,
        "sections": memory_service.sections(content),
        "profile": profile,
        "reflection": reflection,
        "feedback": feedback.get("items") or [],
        "meta": memory_service.status(),
    }


@router.delete("/memory")
async def reset_memory():
    try:
        content = await memory_service.reset_memory()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "content": content}


@router.get("/feedback")
async def get_feedback(
    limit: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await feedback_service.list_feedback(db, limit=limit, status=status)


@router.get("/feedback/reasons")
async def get_feedback_reasons():
    return {"items": feedback_service.reasons()}


@router.post("/feedback", status_code=201)
async def create_feedback(body: FeedbackCreate, db: AsyncSession = Depends(get_db)):
    try:
        feedback = await feedback_service.create_feedback(
            db,
            target_type=body.target_type,
            target_id=body.target_id,
            conversation_id=body.conversation_id,
            rating=body.rating,
            reason=body.reason,
            comment=body.comment,
        )
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("保存反馈失败：%s", exc)
        raise HTTPException(status_code=500, detail=f"保存反馈失败：{exc}")

    feedback_service.schedule_reflection()
    return {
        "ok": True,
        "id": feedback.id,
        "status": feedback.status,
        "memory_updated": feedback.status == "noted",
        "message": "已记录。助手会在后台消化这条反馈，并调整后续采集与推荐。",
    }
