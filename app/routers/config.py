import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, BASE_DIR
from app.database import get_db
from app.env_store import read_env_file, write_env_file
from app.services.prompts import build_base_prompt, DEFAULT_EXT_PROMPT
from app.services.model_configs import get_configs, get_enabled_config, config_to_dict, decrypt_api_key

router = APIRouter(prefix="/api/config", tags=["config"])

IMAGES_DIR = BASE_DIR / "images"
ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ConfigUpdate(BaseModel):
    llm_configs: list | None = None
    active_llm: str | None = None
    search_configs: list | None = None
    active_search: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    search_base_url: str | None = None
    search_api_key: str | None = None
    search_cron: str | None = None
    task_mode: str | None = None
    task_interval_hours: int | None = None
    task_start_hour: int | None = None
    task_custom_cron: str | None = None
    task_selected_categories: list | None = None
    agent_ext_mode: str | None = None
    agent_ext_prompt: str | None = None
    agent_recommend_enabled: bool | None = None
    agent_brief_enabled: bool | None = None
    agent_cover_enabled: bool | None = None
    work_mode: str | None = None


class ConfigResponse(BaseModel):
    llm_configs: list = []
    image_configs: list = []
    search_configs: list = []
    active_llm: str = ""
    active_image: str = ""
    active_search: str = ""
    nickname: str = ""
    avatar_url: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    search_base_url: str = ""
    search_cron: str = ""
    has_llm_key: bool = False
    has_search_key: bool = False
    task_mode: str = "preset"
    task_interval_hours: int = 8
    task_start_hour: int = 0
    task_custom_cron: str = ""
    task_selected_categories: list = []
    agent_base_prompt: str = ""
    agent_ext_preset: str = ""
    agent_ext_mode: str = "preset"
    agent_ext_prompt: str = ""
    agent_recommend_enabled: bool = False
    agent_brief_enabled: bool = False
    agent_cover_enabled: bool = False
    work_mode: str = ""
    db_mode: str = "system"
    db_config: dict = {}
    db_complete: bool = False


def _parse_json_list(raw: str) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return list(parsed.values())
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return []


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def derive_task_cron(mode: str, interval_hours: int, start_hour: int, custom_cron: str) -> str:
    if mode == "custom":
        return custom_cron.strip() or "0 */6 * * *"
    interval_hours = max(1, interval_hours)
    if interval_hours == 1:
        return "0 * * * *"
    if start_hour <= 0:
        return f"0 */{interval_hours} * * *"
    return f"0 {start_hour}/{interval_hours} * * *"


@router.get("", response_model=ConfigResponse)
async def get_config(db: AsyncSession = Depends(get_db)):
    from app.services import db_service

    env_vars = read_env_file()
    db_state = db_service.get_db_state()

    interval_hours = _parse_int(env_vars.get("TASK_INTERVAL_HOURS", ""), 8)
    selected_categories = _parse_json_list(env_vars.get("TASK_SELECTED_CATEGORIES", ""))
    selected_categories = [str(x) for x in selected_categories]

    llm_configs = [config_to_dict(c) for c in await get_configs(db, "llm")]
    image_configs = [config_to_dict(c) for c in await get_configs(db, "image")]
    search_configs = [config_to_dict(c) for c in await get_configs(db, "search")]

    llm_enabled = await get_enabled_config(db, "llm")
    image_enabled = await get_enabled_config(db, "image")
    search_enabled = await get_enabled_config(db, "search")

    return ConfigResponse(
        llm_configs=llm_configs,
        image_configs=image_configs,
        search_configs=search_configs,
        active_llm=str(llm_enabled.id) if llm_enabled else "",
        active_image=str(image_enabled.id) if image_enabled else "",
        active_search=str(search_enabled.id) if search_enabled else "",
        nickname=env_vars.get("NICKNAME", ""),
        avatar_url=env_vars.get("AVATAR_URL", ""),
        llm_base_url=llm_enabled.base_url if llm_enabled else "",
        llm_model=llm_enabled.model_id if llm_enabled else "",
        search_base_url=search_enabled.base_url if search_enabled else "",
        search_cron=env_vars.get("SEARCH_CRON", "0 */6 * * *"),
        has_llm_key=bool(llm_enabled and decrypt_api_key(llm_enabled.api_key_enc)),
        has_search_key=bool(search_enabled and decrypt_api_key(search_enabled.api_key_enc)),
        task_mode=env_vars.get("TASK_MODE", "preset"),
        task_interval_hours=interval_hours,
        task_start_hour=_parse_int(env_vars.get("TASK_START_HOUR", ""), 0),
        task_custom_cron=env_vars.get("TASK_CUSTOM_CRON", ""),
        task_selected_categories=selected_categories,
        agent_base_prompt=build_base_prompt(interval_hours, selected_categories),
        agent_ext_preset=DEFAULT_EXT_PROMPT,
        agent_ext_mode=env_vars.get("AGENT_EXT_MODE", "preset"),
        agent_ext_prompt=env_vars.get("AGENT_EXT_PROMPT", ""),
        agent_recommend_enabled=env_vars.get("AGENT_RECOMMEND_ENABLED", "") == "true",
        agent_brief_enabled=env_vars.get("AGENT_BRIEF_ENABLED", "") == "true",
        agent_cover_enabled=env_vars.get("AGENT_COVER_ENABLED", "") == "true",
        work_mode=env_vars.get("WORK_MODE", ""),
        db_mode=db_state["mode"],
        db_config=db_state["config"],
        db_complete=db_state["complete"],
    )


@router.put("")
async def update_config(config: ConfigUpdate):
    env_vars = read_env_file()

    task_touched = any(v is not None for v in (
        config.task_mode, config.task_interval_hours, config.task_start_hour,
        config.task_custom_cron, config.task_selected_categories,
    ))

    if config.task_mode is not None:
        env_vars["TASK_MODE"] = config.task_mode
    if config.task_interval_hours is not None:
        env_vars["TASK_INTERVAL_HOURS"] = str(config.task_interval_hours)
    if config.task_start_hour is not None:
        env_vars["TASK_START_HOUR"] = str(config.task_start_hour)
    if config.task_custom_cron is not None:
        env_vars["TASK_CUSTOM_CRON"] = config.task_custom_cron
    if config.task_selected_categories is not None:
        env_vars["TASK_SELECTED_CATEGORIES"] = json.dumps(config.task_selected_categories, ensure_ascii=False)

    if config.agent_ext_mode is not None:
        env_vars["AGENT_EXT_MODE"] = config.agent_ext_mode
    if config.agent_ext_prompt is not None:
        env_vars["AGENT_EXT_PROMPT"] = config.agent_ext_prompt
    if config.agent_recommend_enabled is not None:
        env_vars["AGENT_RECOMMEND_ENABLED"] = "true" if config.agent_recommend_enabled else "false"
    if config.agent_brief_enabled is not None:
        env_vars["AGENT_BRIEF_ENABLED"] = "true" if config.agent_brief_enabled else "false"
    if config.agent_cover_enabled is not None:
        env_vars["AGENT_COVER_ENABLED"] = "true" if config.agent_cover_enabled else "false"
    if config.work_mode is not None:
        env_vars["WORK_MODE"] = config.work_mode

    if task_touched:
        env_vars["SEARCH_CRON"] = derive_task_cron(
            env_vars.get("TASK_MODE", "preset"),
            _parse_int(env_vars.get("TASK_INTERVAL_HOURS", ""), 8),
            _parse_int(env_vars.get("TASK_START_HOUR", ""), 0),
            env_vars.get("TASK_CUSTOM_CRON", ""),
        )

    if config.nickname is not None:
        env_vars["NICKNAME"] = config.nickname
    if config.avatar_url is not None:
        env_vars["AVATAR_URL"] = config.avatar_url
    if config.llm_base_url is not None:
        env_vars["LLM_BASE_URL"] = config.llm_base_url
    if config.llm_api_key is not None:
        env_vars["LLM_API_KEY"] = config.llm_api_key
    if config.llm_model is not None:
        env_vars["LLM_MODEL"] = config.llm_model
    if config.search_base_url is not None:
        env_vars["SEARCH_BASE_URL"] = config.search_base_url
    if config.search_api_key is not None:
        env_vars["SEARCH_API_KEY"] = config.search_api_key
    if config.search_cron is not None:
        env_vars["SEARCH_CRON"] = config.search_cron

    write_env_file(env_vars)

    settings.SEARCH_CRON = env_vars.get("SEARCH_CRON", "0 */6 * * *")

    return {"message": "ok"}


@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AVATAR_EXT:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPG/GIF/WebP 图片")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片大小不能超过 5MB")

    filename = f"avatar{ext}"
    IMAGES_DIR.mkdir(exist_ok=True)
    (IMAGES_DIR / filename).write_bytes(content)

    url = f"/images/{filename}?v={int(time.time())}"

    env_vars = read_env_file()
    env_vars["AVATAR_URL"] = url
    write_env_file(env_vars)

    return {"avatar_url": url}
