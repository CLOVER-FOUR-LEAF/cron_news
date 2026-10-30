"""Agent 之间的任务调度（Agent-to-Agent）。

对话助手不直接实现采集/简报/推荐，而是通过这里把任务委派给对应的专职 Agent：

- 采集 Agent：`run_search_task`（搜索 + 撰稿 + 封面 + 向量索引）
- 简报 Agent：`run_brief_task`
- 推荐 Agent：`run_recommend_task`

所有 Agent 任务（包括调度器自己的定时任务）共用一把锁，避免同时写库、重复调用模型。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from app.database import get_session
from app.logging_setup import get_logger
from app.services import embeddings
from app.services.ai_service import run_search_task
from app.services.brief_service import run_brief_task
from app.services.recommend_service import run_recommend_task

logger = get_logger("agent_jobs")

_job_lock = asyncio.Lock()
_current_job: str | None = None
_last_job: dict | None = None


class JobBusy(RuntimeError):
    """已有 Agent 任务在跑。"""

    def __init__(self, message: str):
        super().__init__(message)


def busy() -> bool:
    return _job_lock.locked()


def _assist_blocked() -> dict | None:
    """辅助模式下平台不运行内置 Agent；对话委派等入口也要拦住。"""
    try:
        from app.services.assist_service import is_assist

        if is_assist():
            return {
                "ok": False,
                "assist_blocked": True,
                "error": "辅助模式下平台不运行内置 Agent；请由外部 Agent 通过 /api/news、/api/brief 处理。",
            }
    except Exception:
        return None
    return None


def status() -> dict:
    return {"busy": busy(), "current": _current_job, "last": _last_job}


@asynccontextmanager
async def job_slot(name: str):
    """独占执行一个 Agent 任务。已被占用时立即抛 JobBusy，而不是排队。"""
    global _current_job

    if _job_lock.locked():
        raise JobBusy(f"另一个 Agent 任务（{_current_job or '未知'}）正在执行，请稍后再试")

    async with _job_lock:
        _current_job = name
        logger.info("Agent 任务开始：%s", name)
        try:
            yield
        finally:
            _current_job = None


async def _run_job(name: str, worker) -> dict:
    global _last_job

    started = datetime.now()
    try:
        async with job_slot(name):
            result = await worker()
    except JobBusy as exc:
        return {"ok": False, "busy": True, "message": str(exc)}
    except Exception as exc:
        elapsed = (datetime.now() - started).total_seconds()
        _last_job = {
            "name": name,
            "ok": False,
            "started_at": started.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "error": str(exc),
        }
        logger.exception("Agent 任务失败：%s：%s", name, exc)
        return {"ok": False, "job": name, "error": str(exc)}

    elapsed = (datetime.now() - started).total_seconds()
    _last_job = {
        "name": name,
        "ok": True,
        "started_at": started.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "result": result,
    }
    logger.info("Agent 任务完成：%s，耗时 %.1fs，结果=%s", name, elapsed, result)
    return {"ok": True, "job": name, "elapsed_seconds": round(elapsed, 1), "result": result}


async def run_collection(
    emit=None,
    tags: list[str] | None = None,
    interval_hours: int | None = None,
) -> dict:
    """委派给采集 Agent：搜索 → 撰稿 → 封面 → 向量索引。"""
    blocked = _assist_blocked()
    if blocked:
        return blocked

    async def worker():
        async with get_session() as session:
            result = await run_search_task(
                session, emit=emit, tags=tags, interval_hours=interval_hours
            )
            await session.commit()
        await embeddings.backfill()
        return result

    return await _run_job("采集", worker)


async def run_brief(emit=None) -> dict:
    """委派给简报 Agent：把当日全部新闻汇总成一篇统一简报。"""
    blocked = _assist_blocked()
    if blocked:
        return blocked

    async def worker():
        async with get_session() as session:
            result = await run_brief_task(session, emit=emit)
            await session.commit()
        return result

    return await _run_job("简报", worker)


async def run_recommend(emit=None) -> dict:
    """委派给推荐 Agent：计算新闻相关性。"""
    blocked = _assist_blocked()
    if blocked:
        return blocked

    async def worker():
        async with get_session() as session:
            result = await run_recommend_task(session, emit=emit)
            await session.commit()
        return result

    return await _run_job("推荐", worker)
