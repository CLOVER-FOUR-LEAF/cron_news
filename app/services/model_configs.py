"""模型配置服务：大语言 / 文生图 / 搜索 三类配置统一入库，API Key 使用 Fernet 对称加密后存储。

加密密钥存放在 .env 的 SECRET_KEY（首次启动自动生成），配合数据库持久化。
"""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig
from app.env_store import read_env_file, write_env_file
from app.services.provider_presets import find_preset, provider_key_for

CONFIG_TYPES = {
    "llm": "大语言",
    "image": "文生图",
    "search": "搜索",
    "embedding": "向量",
}

SECRET_KEY_ENV = "SECRET_KEY"


def _secret_key() -> bytes:
    env = read_env_file()
    key = env.get(SECRET_KEY_ENV)
    if not key:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        env[SECRET_KEY_ENV] = key
        write_env_file(env)
    return key.encode()


def encrypt_api_key(value: str) -> str:
    if not value:
        return ""
    from cryptography.fernet import Fernet

    return Fernet(_secret_key()).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_api_key(enc: str | None) -> str:
    if not enc:
        return ""
    from cryptography.fernet import Fernet

    try:
        return Fernet(_secret_key()).decrypt(enc.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


async def get_configs(db: AsyncSession, config_type: str) -> list[ModelConfig]:
    result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.config_type == config_type)
        .order_by(ModelConfig.enabled.desc(), ModelConfig.id.desc())
    )
    return list(result.scalars().all())


async def get_enabled_config(db: AsyncSession, config_type: str) -> ModelConfig | None:
    result = await db.execute(
        select(ModelConfig).where(ModelConfig.config_type == config_type, ModelConfig.enabled.is_(True))
    )
    return result.scalars().first()


async def get_active_endpoint(db: AsyncSession, config_type: str) -> dict | None:
    """返回当前启用的 {config_type} 配置对应的 {base_url, api_key, model_id}，无启用配置时返回 None。"""
    cfg = await get_enabled_config(db, config_type)
    if not cfg or not cfg.base_url:
        return None
    api_key = decrypt_api_key(cfg.api_key_enc)
    return {
        "base_url": cfg.base_url,
        "api_key": api_key,
        "model_id": cfg.model_id,
        "provider": cfg.provider,
        "provider_name": cfg.provider_name or cfg.provider,
        "name": cfg.name,
        "id": cfg.id,
        "native_search": bool(cfg.native_search),
        "native_search_kind": cfg.native_search_kind or "",
        "native_search_detail": cfg.native_search_detail or "",
        "native_search_checked_at": (
            cfg.native_search_checked_at.isoformat() if cfg.native_search_checked_at else None
        ),
    }


async def set_disabled(db: AsyncSession, config_id: int) -> ModelConfig | None:
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    target = result.scalars().first()
    if not target:
        return None
    target.enabled = False
    await db.flush()
    return target


async def set_enabled(db: AsyncSession, config_id: int) -> ModelConfig | None:
    """启用指定配置，并关闭同类型其它配置。"""
    result = await db.execute(select(ModelConfig).where(ModelConfig.id == config_id))
    target = result.scalars().first()
    if not target:
        return None
    others = await db.execute(
        select(ModelConfig).where(
            ModelConfig.config_type == target.config_type,
            ModelConfig.id != target.id,
        )
    )
    for cfg in others.scalars().all():
        cfg.enabled = False
    target.enabled = True
    await db.flush()
    return target


def config_to_dict(cfg: ModelConfig) -> dict:
    key = decrypt_api_key(cfg.api_key_enc)
    preset_key = ""
    if cfg.config_type in {"llm", "image", "search"}:
        preset = find_preset(cfg.config_type, cfg.provider, cfg.base_url, cfg.model_id)
        if preset:
            preset_key = preset["key"]
    native_available = False
    native_kind = cfg.native_search_kind or ""
    if cfg.config_type == "llm":
        from app.services.native_search import capability_available, resolve_spec

        native_available = bool(key) and bool(cfg.base_url) and capability_available(
            cfg.provider,
            cfg.base_url,
            cfg.model_id,
            bool(cfg.native_search),
            cfg.native_search_checked_at,
        )
        if not native_kind:
            spec = resolve_spec(cfg.provider, cfg.base_url, cfg.model_id)
            if spec:
                native_kind = spec.kind
    requires_key = True
    if cfg.config_type == "search":
        from app.services.search_providers import provider_requires_key

        requires_key = provider_requires_key(cfg.provider, cfg.base_url)
    return {
        "id": cfg.id,
        "provider": cfg.provider,
        "provider_name": cfg.provider_name or cfg.provider,
        "provider_key": preset_key or provider_key_for(cfg.config_type, cfg.provider, cfg.base_url),
        "name": cfg.name,
        "base_url": cfg.base_url,
        "model_id": cfg.model_id,
        "config_type": cfg.config_type,
        "enabled": bool(cfg.enabled),
        "has_api_key": bool(key),
        "requires_key": requires_key,
        "env_key": cfg.env_key,
        "native_search": bool(cfg.native_search),
        "native_available": native_available,
        "native_search_kind": native_kind,
        "native_search_detail": cfg.native_search_detail or "",
        "native_search_checked_at": (
            cfg.native_search_checked_at.isoformat() if cfg.native_search_checked_at else None
        ),
    }


async def get_effective_search_endpoint(db: AsyncSession) -> dict | None:
    """返回当前实际可用的搜索端点。

    优先使用已启用的独立搜索服务；如果没有（或未填 Key），回退到大语言模型自带的联网搜索。
    返回值额外带 source=service|native，方便界面展示与调度判断。
    """
    from app.services.search_providers import resolve_provider

    cfg = await get_enabled_config(db, "search")
    if cfg and cfg.base_url:
        api_key = decrypt_api_key(cfg.api_key_enc)
        provider = resolve_provider(cfg.provider, cfg.base_url)
        if not provider.requires_key() or api_key:
            return {
                "source": "service",
                "id": cfg.id,
                "provider": cfg.provider,
                "provider_name": cfg.provider_name or cfg.provider,
                "name": cfg.name,
                "base_url": cfg.base_url,
                "api_key": api_key,
                "model_id": cfg.model_id,
                "label": f"{cfg.provider_name or cfg.provider} 搜索服务",
            }

    llm = await get_enabled_config(db, "llm")
    if not llm or not llm.base_url or not llm.api_key_enc:
        return None
    llm_key = decrypt_api_key(llm.api_key_enc)
    if not llm_key:
        return None
    from app.services.native_search import capability_available, resolve_spec

    if not capability_available(
        llm.provider,
        llm.base_url,
        llm.model_id,
        bool(llm.native_search),
        llm.native_search_checked_at,
    ):
        return None
    spec = resolve_spec(llm.provider, llm.base_url, llm.model_id)
    label = spec.label if spec else (llm.native_search_kind or "大模型内置搜索")
    return {
        "source": "native",
        "id": llm.id,
        "provider": llm.provider,
        "provider_name": llm.provider_name or llm.provider,
        "name": llm.name,
        "base_url": llm.base_url,
        "api_key": llm_key,
        "model_id": llm.model_id,
        "native_kind": spec.kind if spec else (llm.native_search_kind or ""),
        "label": label,
    }


async def search_endpoint_status(db: AsyncSession) -> dict:
    endpoint = await get_effective_search_endpoint(db)
    if not endpoint:
        return {
            "ready": False,
            "source": "none",
            "label": "",
            "detail": "未配置可用的搜索服务，且当前大模型没有检测到内置联网搜索",
        }
    return {
        "ready": True,
        "source": endpoint["source"],
        "label": endpoint.get("label", ""),
        "detail": (
            "正在使用大模型内置联网搜索"
            if endpoint["source"] == "native"
            else "正在使用独立搜索服务"
        ),
    }


def reset_native_search(cfg: ModelConfig) -> None:
    """配置变更后清空旧的联网检测结论，等待重新探测。"""
    from app.services.native_search import resolve_spec

    spec = resolve_spec(cfg.provider, cfg.base_url, cfg.model_id)
    cfg.native_search = False
    cfg.native_search_kind = spec.kind if spec else ""
    cfg.native_search_detail = ""
    cfg.native_search_checked_at = None


async def detect_and_store_native_search(
    db: AsyncSession,
    cfg: ModelConfig,
    api_key: str | None = None,
    *,
    timeout: float = 15.0,
) -> dict:
    """实际探测大模型内置搜索并写回配置表。"""
    from app.services.native_search import detect_native_search, resolve_spec

    key = api_key if api_key is not None else decrypt_api_key(cfg.api_key_enc)
    spec = resolve_spec(cfg.provider, cfg.base_url, cfg.model_id)
    if not key:
        result = {
            "available": False,
            "kind": spec.kind if spec else "",
            "label": spec.label if spec else "",
            "detail": "未填写 API Key，无法检测内置联网搜索",
        }
    else:
        result = await detect_native_search(
            cfg.provider, cfg.base_url, key, cfg.model_id, timeout=timeout
        )
    cfg.native_search = bool(result.get("available"))
    cfg.native_search_kind = str(result.get("kind") or (spec.kind if spec else ""))
    cfg.native_search_detail = str(result.get("detail") or "")[:300]
    cfg.native_search_checked_at = datetime.now()
    await db.flush()
    return result


async def prepare_llm_native_search(
    db: AsyncSession,
    cfg: ModelConfig,
    api_key: str | None = None,
    *,
    probe_standalone: bool = True,
) -> None:
    """新建/修改/启用 LLM 配置后更新联网能力。

    独立搜索接口（DeepSeek/Kimi/智谱）保存时顺手探测；MiMo 这类对话工具型必须显式测试，
    避免保存动作被一次 20 秒的模型调用拖住。
    """
    from app.services.native_search import resolve_spec

    reset_native_search(cfg)
    spec = resolve_spec(cfg.provider, cfg.base_url, cfg.model_id)
    if not spec:
        return
    key = api_key if api_key is not None else decrypt_api_key(cfg.api_key_enc)
    if spec.mode == "standalone" and probe_standalone and key:
        await detect_and_store_native_search(db, cfg, key, timeout=10.0)
    elif spec.mode == "chat_tool":
        cfg.native_search_detail = "保存后请点「测试连接」检测内置联网（MiMo 需先在控制台开通联网插件）"
        await db.flush()


async def migrate_legacy_configs(db: AsyncSession) -> bool:
    """将旧版 env 中的 LLM_CONFIGS / SEARCH_CONFIGS JSON 及 API_KEY_* 迁移进 model_configs 表。"""
    from sqlalchemy import func

    env = read_env_file()
    changed = False
    consumed_keys: set[str] = set()

    # 1) 已存在的配置：若 api_key_enc 为空且 env 对应键有值，加密迁移入库存
    result = await db.execute(select(ModelConfig))
    rows = list(result.scalars().all())
    for cfg in rows:
        if not cfg.api_key_enc and cfg.env_key:
            raw = env.get(cfg.env_key, "")
            if raw:
                cfg.api_key_enc = encrypt_api_key(raw)
                consumed_keys.add(cfg.env_key)
                changed = True

    # 2) 首次迁移：从旧 JSON 配置创建实体
    count = await db.execute(select(func.count()).select_from(ModelConfig))
    if count.scalar():
        await db.flush()
        return changed

    added = 0

    def _to_row(entry: dict, config_type: str, enabled_id: str):
        nonlocal added
        if not isinstance(entry, dict):
            return
        provider = str(entry.get("provider", "") or entry.get("name", "") or "自定义")
        base_url = str(entry.get("url", "") or "")
        model_id = str(entry.get("model", "") or "")
        api_key = str(entry.get("api_key", "") or "")
        db.add(ModelConfig(
            provider=provider,
            provider_name=provider,
            name=model_id or provider,
            base_url=base_url,
            model_id=model_id,
            config_type=config_type,
            enabled=(str(entry.get("id", "")) == enabled_id),
            env_key="",
            api_key_enc=encrypt_api_key(api_key),
        ))
        added += 1

    try:
        llm_list = json.loads(env.get("LLM_CONFIGS", "") or "[]")
    except (json.JSONDecodeError, ValueError):
        llm_list = []
    if isinstance(llm_list, dict):
        llm_list = list(llm_list.values())
    for e in llm_list:
        _to_row(e, "llm", env.get("ACTIVE_LLM", ""))

    try:
        search_list = json.loads(env.get("SEARCH_CONFIGS", "") or "[]")
    except (json.JSONDecodeError, ValueError):
        search_list = []
    if isinstance(search_list, dict):
        search_list = list(search_list.values())
    for e in search_list:
        _to_row(e, "search", env.get("ACTIVE_SEARCH", ""))

    await db.flush()

    # 迁移完成后清理 env 中已被消费的旧 API_KEY_*，保留 SECRET_KEY
    removed = [k for k in consumed_keys if k in env]
    if removed:
        from pathlib import Path

        from app.config import BASE_DIR

        env_file = BASE_DIR / ".env"
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
            keep = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in removed:
                        continue
                keep.append(line)
            env_file.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")

    return changed or added > 0
