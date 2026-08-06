from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.env_store import read_env_file, write_env_file
from app.models import ModelConfig
from app.services.model_configs import (
    CONFIG_TYPES,
    PROVIDERS,
    get_provider,
    providers_for_type,
    set_enabled,
    config_to_dict,
    get_configs,
    api_key_value,
)

router = APIRouter(prefix="/api/model-configs", tags=["model-configs"])


class ModelConfigCreate(BaseModel):
    provider: str
    name: str = ""
    base_url: str = ""
    model_id: str = ""
    config_type: str
    api_key: str = ""
    enabled: bool = False


class ModelConfigUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    api_key: str | None = None


@router.get("/providers")
async def list_providers(config_type: str):
    items = []
    for p in providers_for_type(config_type):
        key = api_key_value(p["env_key"])
        items.append({**p, "has_key": bool(key), "api_key": key})
    return {"items": items}


@router.get("")
async def list_configs(config_type: str, db: AsyncSession = Depends(get_db)):
    if config_type not in CONFIG_TYPES:
        raise HTTPException(status_code=400, detail="不支持的配置类型")
    configs = await get_configs(db, config_type)
    return {"items": [config_to_dict(c) for c in configs]}


@router.post("", status_code=201)
async def create_config(body: ModelConfigCreate, db: AsyncSession = Depends(get_db)):
    if body.config_type not in CONFIG_TYPES:
        raise HTTPException(status_code=400, detail="不支持的配置类型")

    provider = get_provider(body.provider)
    env = read_env_file()

    if provider:
        env_key = provider["env_key"]
        base_url = body.base_url.strip() or provider["base_url"]
        provider_name = provider["name"]
        name = body.name.strip() or body.model_id.strip() or provider["name"]
        if body.api_key.strip():
            env[env_key] = body.api_key.strip()
            write_env_file(env)
    else:
        # 自定义配置
        env_key = ""
        base_url = body.base_url.strip()
        provider_name = body.name.strip() or "自定义"
        name = body.model_id.strip() or body.name.strip() or "自定义"
        if not base_url:
            raise HTTPException(status_code=400, detail="自定义配置必须填写 Base URL")
        if body.api_key.strip():
            from datetime import datetime

            env_key = f"API_KEY_CUST_{datetime.now().strftime('%H%M%S')}"
            env[env_key] = body.api_key.strip()
            write_env_file(env)

    if not base_url:
        raise HTTPException(status_code=400, detail="请填写 Base URL")
    if not body.model_id.strip() and body.config_type != "search":
        raise HTTPException(status_code=400, detail="请填写模型 ID")

    # 同一类型只允许一个启用；启用第一个时自动启用
    existing = await get_configs(db, body.config_type)
    will_enable = body.enabled or len(existing) == 0

    cfg = ModelConfig(
        provider=body.provider,
        provider_name=provider_name,
        name=name,
        base_url=base_url,
        model_id=body.model_id.strip(),
        config_type=body.config_type,
        enabled=will_enable,
        env_key=env_key,
    )
    if will_enable:
        for old in existing:
            old.enabled = False
    db.add(cfg)
    await db.flush()
    await db.refresh(cfg)
    return config_to_dict(cfg)


@router.put("/{config_id}")
async def update_config(config_id: int, body: ModelConfigUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")

    if body.name is not None:
        cfg.name = body.name.strip()
    if body.base_url is not None:
        cfg.base_url = body.base_url.strip()
    if body.model_id is not None:
        cfg.model_id = body.model_id.strip()

    if body.api_key is not None and body.api_key.strip():
        env = read_env_file()
        if not cfg.env_key:
            from datetime import datetime

            cfg.env_key = f"API_KEY_CUST_{datetime.now().strftime('%H%M%S')}"
        env[cfg.env_key] = body.api_key.strip()
        write_env_file(env)

    await db.flush()
    await db.refresh(cfg)
    return config_to_dict(cfg)


@router.delete("/{config_id}")
async def delete_config(config_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    was_enabled = cfg.enabled
    await db.delete(cfg)
    await db.flush()
    if was_enabled:
        remaining = await get_configs(db, cfg.config_type)
        if remaining:
            remaining[0].enabled = True
            await db.flush()
    return {"ok": True}


@router.post("/{config_id}/enable")
async def enable_config(config_id: int, db: AsyncSession = Depends(get_db)):
    cfg = await set_enabled(db, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    await db.refresh(cfg)
    return config_to_dict(cfg)
