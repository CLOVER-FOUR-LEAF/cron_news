"""模型配置服务：大语言 / 文生图 / 搜索 三类配置统一入库，env 仅存各家 apikey。"""

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

# 提供商注册表：id / 显示名 / 默认 base_url / 支持类型 / env 中 apikey 键名
PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "types": ["llm"], "env_key": "API_KEY_DEEPSEEK"},
    {"id": "kimi", "name": "Kimi", "base_url": "https://api.moonshot.cn/v1", "types": ["llm"], "env_key": "API_KEY_KIMI"},
    {"id": "zhipu", "name": "智谱AI", "base_url": "https://open.bigmodel.cn/api/paas/v4", "types": ["llm"], "env_key": "API_KEY_ZHIPU"},
    {"id": "minimax", "name": "MiniMax", "base_url": "https://api.minimax.chat/v1", "types": ["llm"], "env_key": "API_KEY_MINIMAX"},
    {"id": "mimo", "name": "MiMo", "base_url": "https://api.mimo.xiaomi.com/v1", "types": ["llm", "search"], "env_key": "API_KEY_MIMO"},
    {"id": "bailian", "name": "阿里云百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "types": ["llm", "image"], "env_key": "API_KEY_BAILIAN"},
    {"id": "volcano", "name": "字节方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "types": ["llm", "image"], "env_key": "API_KEY_VOLCANO"},
    {"id": "hunyuan", "name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "types": ["llm"], "env_key": "API_KEY_HUNYUAN"},
    {"id": "tavily", "name": "Tavily", "base_url": "https://api.tavily.com", "types": ["search"], "env_key": "API_KEY_TAVILY"},
]


def get_provider(provider_id: str) -> dict | None:
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return None


def providers_for_type(config_type: str) -> list[dict]:
    return [p for p in PROVIDERS if config_type in p["types"]]


def api_key_value(env_key: str) -> str:
    return read_env_file().get(env_key, "")


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
    api_key = api_key_value(cfg.env_key)
    return {
        "base_url": cfg.base_url,
        "api_key": api_key,
        "model_id": cfg.model_id,
        "provider": cfg.provider,
        "provider_name": cfg.provider_name,
        "name": cfg.name,
        "id": cfg.id,
    }


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


async def migrate_legacy_configs(db: AsyncSession) -> bool:
    """将旧版 env 中的 LLM_CONFIGS / SEARCH_CONFIGS JSON 迁移进 model_configs 表。"""
    from sqlalchemy import func

    count = (await db.execute(select(func.count()).select_from(ModelConfig))).scalar()
    if count:
        return False

    env = read_env_file()
    env_changed = False
    added = 0

    def _to_row(entry: dict, config_type: str, enabled_id: str):
        nonlocal env_changed, added
        if not isinstance(entry, dict):
            return
        provider = str(entry.get("provider", "custom"))
        p = get_provider(provider)
        api_key = str(entry.get("api_key", "") or "")
        if p:
            env_key = p["env_key"]
            provider_name = p["name"]
            base_url = str(entry.get("url", "") or p["base_url"])
            name = str(entry.get("model", "") or entry.get("name", "") or p["name"])
        else:
            from datetime import datetime

            env_key = f"API_KEY_CUST_{datetime.now().strftime('%H%M%S')}"
            provider_name = str(entry.get("name", "") or provider)
            base_url = str(entry.get("url", ""))
            name = str(entry.get("model", "") or entry.get("name", "") or provider_name)
        if api_key and not env.get(env_key):
            env[env_key] = api_key
            env_changed = True
        db.add(ModelConfig(
            provider=provider,
            provider_name=provider_name,
            name=name,
            base_url=base_url,
            model_id=str(entry.get("model", "") or ""),
            config_type=config_type,
            enabled=(str(entry.get("id", "")) == enabled_id),
            env_key=env_key,
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
    if env_changed:
        write_env_file(env)
    return added > 0


def config_to_dict(cfg: ModelConfig) -> dict:
    key = api_key_value(cfg.env_key)
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
