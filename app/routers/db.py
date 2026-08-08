import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import db_service

router = APIRouter(prefix="/api/db", tags=["db"])


class DBConfigModel(BaseModel):
    db_type: str = "mysql"
    host: str = ""
    port: int = 3306
    name: str = ""
    user: str = ""
    password: str = ""


class SwitchModel(BaseModel):
    mode: str
    keep_data: bool = True


@router.post("/test")
async def test_db(config: DBConfigModel):
    ok, msg = await db_service.test_connection(config.model_dump())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/save")
async def save_db(config: DBConfigModel):
    db_service.save_config(config.model_dump())
    return {"ok": True}


class PrecheckModel(BaseModel):
    mode: str = "standalone"


@router.post("/precheck")
async def precheck_db(body: PrecheckModel):
    """切换前预检：目标库是否已存在业务数据（决定「直接切换」还是「迁移」）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import settings

    state = db_service.get_db_state()
    try:
        if body.mode == "standalone":
            if not state["complete"]:
                return {"ok": False, "has_data": False, "message": "独立数据库配置不完整"}
            ok, msg = await db_service.test_connection(state["config"])
            if not ok:
                return {"ok": False, "has_data": False, "message": msg}
            url = db_service.build_url(state["config"])
        elif body.mode == "system":
            url = settings.DATABASE_URL
        else:
            return {"ok": False, "has_data": False, "message": "无效的目标模式"}

        engine = create_async_engine(
            url,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        try:
            has_data = await db_service._target_has_data(engine)
        finally:
            await engine.dispose()
        return {"ok": True, "has_data": has_data, "message": ""}
    except Exception as e:
        return {"ok": False, "has_data": False, "message": str(e)}


@router.post("/switch")
async def switch_db(body: SwitchModel):
    if db_service.is_busy():
        raise HTTPException(status_code=409, detail="迁移正在进行中，请稍后再试")
    if body.mode not in ("system", "standalone"):
        raise HTTPException(status_code=400, detail="无效的目标模式")
    state = db_service.get_db_state()
    if body.mode == "standalone" and not state["complete"]:
        raise HTTPException(status_code=400, detail="独立数据库配置不完整")
    if body.mode == state["mode"]:
        raise HTTPException(status_code=400, detail="当前已处于该数据库模式")
    asyncio.create_task(db_service.run_switch(body.mode, body.keep_data))
    return {"started": True}


@router.get("/status")
async def switch_status():
    return db_service.MIGRATION
