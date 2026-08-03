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
