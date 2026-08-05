import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgentRun
from app.services.agent_prompts import get_all_prompts, save_prompts, AGENT_LABELS

router = APIRouter(prefix="/api/agent", tags=["agent"])


class PromptsModel(BaseModel):
    timed: str | None = None
    recommend: str | None = None
    brief: str | None = None


@router.get("/prompts")
async def get_prompts():
    prompts = get_all_prompts()
    return {
        "agents": AGENT_LABELS,
        "prompts": prompts,
    }


@router.put("/prompts")
async def update_prompts(body: PromptsModel):
    save_prompts(body.model_dump())
    return {"ok": True}


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AgentRun).order_by(AgentRun.id.desc()).limit(10))
    runs = result.scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "status": r.status,
            }
            for r in runs
        ]
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalars().first()
    if not run:
        raise HTTPException(status_code=404, detail="日志不存在")
    try:
        lines = json.loads(run.content)
    except json.JSONDecodeError:
        lines = []
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "status": run.status,
        "lines": lines,
    }
