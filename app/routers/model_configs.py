import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.env_store import read_env_file
from app.models import ModelConfig
from app.services.model_configs import (
    CONFIG_TYPES,
    set_enabled,
    set_disabled,
    config_to_dict,
    get_configs,
    encrypt_api_key,
)

router = APIRouter(prefix="/api/model-configs", tags=["model-configs"])


class ModelConfigCreate(BaseModel):
    provider: str
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

    provider = body.provider.strip()
    base_url = body.base_url.strip()
    model_id = body.model_id.strip()

    if not provider:
        raise HTTPException(status_code=400, detail="请填写服务商名称")
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写 Base URL")
    if not model_id and body.config_type != "search":
        raise HTTPException(status_code=400, detail="请填写模型 ID")

    existing = await get_configs(db, body.config_type)
    will_enable = body.enabled or len(existing) == 0

    cfg = ModelConfig(
        provider=provider,
        provider_name=provider,
        name=model_id or provider,
        base_url=base_url,
        model_id=model_id,
        config_type=body.config_type,
        enabled=will_enable,
        api_key_enc=encrypt_api_key(body.api_key.strip()),
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
        if cfg.name == cfg.model_id or not cfg.name:
            cfg.name = body.model_id.strip()

    # API Key 仅在传入非空时更新（留空表示不修改）
    if body.api_key is not None and body.api_key.strip():
        cfg.api_key_enc = encrypt_api_key(body.api_key.strip())

    await db.flush()
    await db.refresh(cfg)
    return config_to_dict(cfg)


@router.delete("/{config_id}")
async def delete_config(config_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    if cfg.enabled:
        raise HTTPException(status_code=400, detail="已启用的配置不能删除，请先取消启用")
    await db.delete(cfg)
    await db.flush()
    return {"ok": True}


@router.post("/{config_id}/enable")
async def enable_config(config_id: int, db: AsyncSession = Depends(get_db)):
    cfg = await set_enabled(db, config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    await db.refresh(cfg)
    return config_to_dict(cfg)


@router.post("/{config_id}/disable")
async def disable_config(config_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    if cfg.enabled and cfg.config_type == "image":
        env = read_env_file()
        if env.get("AGENT_COVER_ENABLED", "") == "true":
            raise HTTPException(
                status_code=400,
                detail="自动生成资讯封面功能已开启，请先关闭该功能后再取消启用当前文生图模型",
            )
    await set_disabled(db, config_id)
    await db.refresh(cfg)
    return config_to_dict(cfg)


class TestConnectionModel(BaseModel):
    provider: str = ""
    base_url: str = ""
    model_id: str = ""
    config_type: str
    api_key: str = ""


async def _probe(config_type: str, base_url: str, api_key: str, model_id: str, provider: str = "") -> tuple[bool, str]:
    if not base_url:
        return False, "Base URL 为必填项"

    if config_type == "search":
        from app.services.search_providers import probe_providers

        return await probe_providers(base_url=base_url, api_key=api_key or "")

    if not api_key:
        return False, "Base URL 与 API Key 为必填项"

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if config_type == "image":
            try:
                resp = await client.get(f"{base_url}/models", headers=headers)
                if resp.status_code < 400:
                    return True, "连接成功"
            except Exception:
                pass
            resp = await client.get(f"{base_url.rstrip('/')}", headers=headers)
            if resp.status_code < 500:
                return True, "连接成功（基础地址可达）"
            return False, f"连接失败（{resp.status_code}）：{resp.text[:120]}"

        # 大语言模型：轻量 chat completions
        if not model_id:
            return False, "请填写模型 ID"
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
            },
        )
        if resp.status_code < 400:
            return True, "连接成功"
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:120])
        except Exception:
            detail = resp.text[:120]
        return False, f"连接失败（{resp.status_code}）：{detail}"


@router.post("/test")
async def test_connection(body: TestConnectionModel):
    if body.config_type not in CONFIG_TYPES:
        raise HTTPException(status_code=400, detail="不支持的配置类型")
    try:
        ok, message = await _probe(
            body.config_type,
            body.base_url.strip(),
            body.api_key.strip(),
            body.model_id.strip(),
            body.provider.strip(),
        )
    except httpx.HTTPError as e:
        ok, message = False, f"网络错误：{e}"
    except Exception as e:
        ok, message = False, f"测试失败：{e}"
    return {"ok": ok, "message": message}
