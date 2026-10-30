"""对话助手 API：会话 CRUD + SSE 流式问答。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.database import get_db
from app.logging_setup import get_logger
from app.services import assist_service, conversation_service
from app.services.agent import runtime

logger = get_logger("chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    conversation_id: int | None = None
    message: str = Field(..., min_length=1, max_length=8000)


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


def _conversation_dict(conversation) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "message_count": conversation.message_count,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
    }


def _message_dict(message) -> dict:
    def _loads(value):
        if not value:
            return [] if value == "" else None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "citations": _loads(message.citations),
        "tool_trace": _loads(message.tool_calls),
        "usage": _loads(message.usage),
        "status": message.status,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    items = await conversation_service.list_conversations(db, limit=limit, offset=offset)
    return {"items": [_conversation_dict(item) for item in items]}


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, db: AsyncSession = Depends(get_db)):
    conversation = await conversation_service.create_conversation(db, body.title or "新对话")
    return _conversation_dict(conversation)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, db: AsyncSession = Depends(get_db)):
    conversation = await conversation_service.get_conversation(db, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _conversation_dict(conversation)


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: int,
    body: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
):
    conversation = await conversation_service.update_conversation(
        db, conversation_id, title=body.title
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _conversation_dict(conversation)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, db: AsyncSession = Depends(get_db)):
    ok = await conversation_service.soft_delete_conversation(db, conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    conversation = await conversation_service.get_conversation(db, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await conversation_service.get_messages(db, conversation_id, limit, before_id)
    return {"items": [_message_dict(message) for message in messages]}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """非流式一轮对话（供脚本/外部集成使用）。"""
    if assist_service.is_assist():
        raise HTTPException(
            status_code=409,
            detail="辅助模式下平台不运行对话助手；请由外部 Agent 处理问答。",
        )
    conversation = await conversation_service.get_conversation(db, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    result = await runtime.run_turn(conversation_id, body.message, emit=None)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request):
    """SSE 流式对话。注意：不要用 Depends(get_db)，否则会持有 session 到流结束。"""
    if assist_service.is_assist():
        raise HTTPException(
            status_code=409,
            detail="辅助模式下平台不运行对话助手；请由外部 Agent 处理问答。",
        )

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: str, data: dict):
            await queue.put((event, data))

        async def runner():
            try:
                await runtime.run_turn(body.conversation_id, body.message, emit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("对话执行失败：%s", exc)
                await emit("error", {"code": "internal", "message": f"对话执行失败：{exc}"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                if item is None:
                    break
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
