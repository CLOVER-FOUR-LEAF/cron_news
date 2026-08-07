"""模型配置服务：大语言 / 文生图 / 搜索 三类配置统一入库，API Key 使用 Fernet 对称加密后存储。

加密密钥存放在 .env 的 SECRET_KEY（首次启动自动生成），配合数据库持久化。
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig
from app.env_store import read_env_file, write_env_file

CONFIG_TYPES = {
    "llm": "大语言",
    "image": "文生图",
    "search": "搜索",
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
    return {
        "id": cfg.id,
        "provider": cfg.provider,
        "provider_name": cfg.provider_name or cfg.provider,
        "name": cfg.name,
        "base_url": cfg.base_url,
        "model_id": cfg.model_id,
        "config_type": cfg.config_type,
        "enabled": bool(cfg.enabled),
        "has_api_key": bool(key),
        "env_key": cfg.env_key,
    }


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
