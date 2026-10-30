import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.services import assist_service
from app.services.scheduler import scheduler

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
async def get_scheduler_status():
    status = dict(scheduler.status)
    mode = assist_service.work_mode()
    status["work_mode"] = mode
    if mode != "autonomous":
        # 辅助模式不调度；避免切换后展示旧的 next_run
        status["next_run"] = None
    return status


@router.post("/trigger")
async def trigger_search():
    if assist_service.work_mode() != "autonomous":
        raise HTTPException(
            status_code=409,
            detail="当前不是自主模式，平台不执行内置采集；请先切换为自主模式，或由外部 Agent 推送新闻。",
        )
    try:
        result = await scheduler.trigger_now()
        return {"message": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/trigger/stream")
async def trigger_search_stream():
    if assist_service.work_mode() != "autonomous":
        raise HTTPException(
            status_code=409,
            detail="当前不是自主模式，平台不执行内置采集；请先切换为自主模式，或由外部 Agent 推送新闻。",
        )

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event_type: str, text: str, **extra: Any):
            payload = {"type": event_type, "text": text}
            payload.update(extra)
            await queue.put(payload)

        async def run_task():
            try:
                await scheduler.run_with_emit(emit)
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_task())

        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

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
