import httpx
from datetime import datetime
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
    decrypt_api_key,
    detect_and_store_native_search,
    get_configs,
    encrypt_api_key,
    prepare_llm_native_search,
)
from app.services.provider_presets import find_preset, preset_payload

router = APIRouter(prefix="/api/model-configs", tags=["model-configs"])


class ModelConfigCreate(BaseModel):
    provider: str
    provider_name: str = ""
    base_url: str = ""
    model_id: str = ""
    config_type: str
    api_key: str = ""
    enabled: bool = False
    # 新增配置时，「测试连接」检测到的内置联网结果可以随保存一并写入
    native_search: bool | None = None
    native_search_detail: str = ""


class ModelConfigUpdate(BaseModel):
    provider: str | None = None
    provider_name: str | None = None
    name: str | None = None
    base_url: str | None = None
    model_id: str | None = None
    api_key: str | None = None
    native_search: bool | None = None
    native_search_detail: str = ""


@router.get("/presets")
async def list_presets():
    return preset_payload()


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
    provider_name = body.provider_name.strip()
    base_url = body.base_url.strip()
    model_id = body.model_id.strip()

    if not provider:
        raise HTTPException(status_code=400, detail="请填写服务商名称")
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写 Base URL")
    if not model_id and body.config_type != "search":
        raise HTTPException(status_code=400, detail="请填写模型 ID")

    preset = find_preset(body.config_type, provider, base_url, model_id)
    provider_key = preset["key"] if preset else provider
    if not provider_name:
        provider_name = preset["label"] if preset else provider

    existing = await get_configs(db, body.config_type)
    will_enable = body.enabled or len(existing) == 0

    cfg = ModelConfig(
        provider=provider_key,
        provider_name=provider_name,
        name=model_id or provider_name,
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
    # 注意：不要在网络探测前 flush。SQLite 会在第一次写库时持有写锁，
    # 若探测耗时 10 秒，聊天/阅读等并发写入会一直等到 busy_timeout 后失败。
    if body.config_type == "llm":
        await prepare_llm_native_search(
            db, cfg, body.api_key.strip(), probe_standalone=body.native_search is None
        )
        if body.native_search is not None:
            cfg.native_search = bool(body.native_search)
            cfg.native_search_detail = body.native_search_detail[:300]
            cfg.native_search_checked_at = datetime.now()
    await db.flush()
    await db.refresh(cfg)
    return config_to_dict(cfg)


@router.put("/{config_id}")
async def update_config(config_id: int, body: ModelConfigUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")

    changed = False
    native_changed = False

    if body.provider is not None and body.provider.strip():
        new_provider = body.provider.strip()
        if new_provider != cfg.provider:
            cfg.provider = new_provider
            changed = native_changed = True
    if body.provider_name is not None and body.provider_name.strip():
        # 仅修改显示名不应触发联网能力重新探测（尤其是 MiMo 这类需要手动测试的配置）
        cfg.provider_name = body.provider_name.strip()
    if body.name is not None and body.name.strip():
        cfg.name = body.name.strip()
    if body.base_url is not None and body.base_url.strip() != cfg.base_url:
        cfg.base_url = body.base_url.strip()
        changed = native_changed = True
    if body.model_id is not None:
        model_id = body.model_id.strip()
        old_model = cfg.model_id
        if model_id != old_model:
            native_changed = True
            changed = True
            cfg.model_id = model_id
            if not cfg.name or cfg.name == old_model:
                cfg.name = model_id
    if changed:
        preset = find_preset(cfg.config_type, cfg.provider, cfg.base_url, cfg.model_id)
        if preset:
            cfg.provider = preset["key"]
            if body.provider is not None and not body.provider_name and not cfg.provider_name:
                cfg.provider_name = preset["label"] or cfg.provider_name
        elif body.provider is not None:
            cfg.provider_name = cfg.provider_name or cfg.provider

    # API Key 仅在传入非空时更新（留空表示不修改）
    new_key = ""
    if body.api_key is not None and body.api_key.strip():
        new_key = body.api_key.strip()
        cfg.api_key_enc = encrypt_api_key(new_key)
        native_changed = True

    if cfg.config_type == "llm" and (changed or native_changed):
        await prepare_llm_native_search(
            db,
            cfg,
            new_key or decrypt_api_key(cfg.api_key_enc),
            probe_standalone=body.native_search is None,
        )
    # 「测试连接」的结果随保存写回；即使字段没有变化也允许更新，方便用户测完直接保存
    if cfg.config_type == "llm" and body.native_search is not None:
        cfg.native_search = bool(body.native_search)
        cfg.native_search_detail = body.native_search_detail[:300]
        cfg.native_search_checked_at = datetime.now()

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
    if cfg.config_type == "llm" and cfg.api_key_enc and cfg.native_search_checked_at is None:
        from app.services.native_search import resolve_spec

        spec = resolve_spec(cfg.provider, cfg.base_url, cfg.model_id)
        # 先把「启用」落库释放写锁，再做可能长达十几秒的网络探测
        await db.commit()
        if spec and spec.mode == "standalone":
            try:
                await detect_and_store_native_search(
                    db, cfg, decrypt_api_key(cfg.api_key_enc), timeout=12.0
                )
            except Exception as exc:
                cfg.native_search_detail = f"内置搜索检测失败：{str(exc)[:200]}"
                await db.flush()
        elif spec:
            cfg.native_search_kind = spec.kind
            cfg.native_search_detail = "请点「测试连接」检测内置联网能力"
            await db.flush()
    return config_to_dict(cfg)


@router.post("/{config_id}/detect-search")
async def detect_config_search(config_id: int, db: AsyncSession = Depends(get_db)):
    """手动重新检测某个大模型配置的内置联网能力。"""
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    if cfg.config_type != "llm":
        raise HTTPException(status_code=400, detail="仅大语言模型支持内置联网检测")
    if not cfg.api_key_enc:
        raise HTTPException(status_code=400, detail="请先填写 API Key")
    detection = await detect_and_store_native_search(db, cfg, None, timeout=25.0)
    await db.refresh(cfg)
    return {"result": detection, "config": config_to_dict(cfg)}


@router.post("/{config_id}/disable")
async def disable_config(config_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    cfg = result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    if cfg.enabled and cfg.config_type == "image":
        env = read_env_file()
        cover_mode = (env.get("AGENT_COVER_MODE", "") or "").strip().lower()
        using_image_cover = cover_mode == "image" or (
            not cover_mode and env.get("AGENT_COVER_ENABLED", "") == "true"
        )
        if using_image_cover:
            raise HTTPException(
                status_code=400,
                detail="封面生成方式为「文生图」，请先改为「大模型 SVG」或关闭封面生成，再取消启用当前文生图模型",
            )
    await set_disabled(db, config_id)
    await db.refresh(cfg)
    return config_to_dict(cfg)


class TestConnectionModel(BaseModel):
    provider: str = ""
    provider_name: str = ""
    base_url: str = ""
    model_id: str = ""
    config_type: str
    api_key: str = ""
    config_id: int | None = None


async def _probe(config_type: str, base_url: str, api_key: str, model_id: str, provider: str = "") -> tuple[bool, str]:
    if not base_url:
        return False, "Base URL 为必填项"

    if config_type == "search":
        from app.services.search_providers import probe_providers

        return await probe_providers(base_url=base_url, api_key=api_key or "", provider=provider)

    if not api_key:
        return False, "Base URL 与 API Key 为必填项"

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if config_type == "embedding":
            if not model_id:
                return False, "请填写模型 ID"
            resp = await client.post(
                f"{base_url}/embeddings",
                headers=headers,
                json={"model": model_id, "input": ["ping"]},
            )
            if resp.status_code < 400:
                try:
                    dim = len((resp.json().get("data") or [{}])[0].get("embedding") or [])
                except Exception:
                    dim = 0
                return True, f"连接成功（向量维度 {dim}）" if dim else "连接成功"
            return False, f"连接失败（{resp.status_code}）：{resp.text[:120]}"
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
async def test_connection(body: TestConnectionModel, db: AsyncSession = Depends(get_db)):
    if body.config_type not in CONFIG_TYPES:
        raise HTTPException(status_code=400, detail="不支持的配置类型")

    cfg = None
    if body.config_id is not None:
        result = await db.execute(select(ModelConfig).where(ModelConfig.id == body.config_id))
        cfg = result.scalars().first()

    provider = body.provider.strip() or (cfg.provider if cfg else "")
    base_url = body.base_url.strip() or (cfg.base_url if cfg else "")
    model_id = body.model_id.strip() or (cfg.model_id if cfg else "")
    api_key = body.api_key.strip() or (decrypt_api_key(cfg.api_key_enc) if cfg else "")

    native = None
    try:
        ok, message = await _probe(
            body.config_type,
            base_url,
            api_key,
            model_id,
            provider,
        )
    except httpx.HTTPError as e:
        ok, message = False, f"网络错误：{e}"
    except Exception as e:
        ok, message = False, f"测试失败：{e}"

    # 大语言模型：连接成功后顺手检测内置联网搜索。
    # 注意：测试接口保持无副作用——即使用户随后点「取消」，也不能改动已保存配置；
    # 检测结果随「保存」请求一起写回（body.native_search）。
    if body.config_type == "llm" and ok:
        from app.services.native_search import detect_native_search, resolve_spec

        spec = resolve_spec(provider, base_url, model_id)
        if spec:
            try:
                native = await detect_native_search(provider, base_url, api_key, model_id, timeout=25.0)
            except Exception as e:
                native = {
                    "available": False,
                    "kind": spec.kind,
                    "label": spec.label,
                    "detail": f"{spec.label} 检测失败：{str(e)[:200]}",
                }
            message = f"{message}；{native.get('detail') or '未检测到内置联网搜索'}"
        else:
            message = f"{message}；该服务商没有可自动检测的内置联网搜索，请配置独立搜索服务"
    return {"ok": ok, "message": message, "native_search": native}
