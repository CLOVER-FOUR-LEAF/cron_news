import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_db
from app.env_store import read_env_file, write_env_file
from app.logging_setup import get_logger
from app.services.prompts import build_base_prompt, DEFAULT_EXT_PROMPT
from app.services import preferences
from app.services import assist_service
from app.services.model_configs import (
    config_to_dict,
    decrypt_api_key,
    get_configs,
    get_enabled_config,
    search_endpoint_status,
)

router = APIRouter(prefix="/api/config", tags=["config"])

logger = get_logger("config")

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
    task_interval_hours: int | None = None
    task_start_hour: int | None = None
    task_schedule_mode: str | None = None
    task_smart_lead_minutes: int | None = None
    task_selected_tags: list | None = None
    agent_interests_text: str | None = None
    agent_schedule_text: str | None = None
    agent_ext_mode: str | None = None
    agent_ext_prompt: str | None = None
    agent_recommend_enabled: bool | None = None
    agent_brief_enabled: bool | None = None
    agent_cover_enabled: bool | None = None
    agent_cover_mode: str | None = None
    onboarding_done: bool | None = None
    work_mode: str | None = None


class ConfigResponse(BaseModel):
    llm_configs: list = []
    image_configs: list = []
    search_configs: list = []
    embedding_configs: list = []
    active_llm: str = ""
    active_image: str = ""
    active_search: str = ""
    active_embedding: str = ""
    nickname: str = ""
    avatar_url: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    search_base_url: str = ""
    has_llm_key: bool = False
    has_search_key: bool = False
    search_ready: bool = False
    search_source: str = "none"
    search_label: str = ""
    llm_native_search: bool = False
    llm_native_search_label: str = ""
    task_interval_hours: int = 8
    task_start_hour: int = 0
    task_schedule_mode: str = "fixed"
    task_smart_lead_minutes: int = 45
    task_selected_tags: list = []
    agent_interests_text: str = ""
    agent_interests_tags: list = []
    agent_schedule_text: str = ""
    agent_schedule_plan: dict = {}
    agent_items_per_run: int = 12
    agent_preferences_analyzed_at: str = ""
    agent_base_prompt: str = ""
    agent_ext_preset: str = ""
    agent_ext_mode: str = "preset"
    agent_ext_prompt: str = ""
    agent_recommend_enabled: bool = False
    agent_brief_enabled: bool = False
    agent_cover_enabled: bool = False
    agent_cover_mode: str = "off"
    onboarding_done: bool = False
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


def _cover_mode(env_vars: dict[str, str]) -> str:
    raw = (env_vars.get("AGENT_COVER_MODE", "") or "").strip().lower()
    if raw in {"image", "svg", "off"}:
        return raw
    return "image" if env_vars.get("AGENT_COVER_ENABLED", "") == "true" else "off"


@router.get("", response_model=ConfigResponse)
async def get_config(db: AsyncSession = Depends(get_db)):
    from app.services import db_service

    env_vars = read_env_file()
    db_state = db_service.get_db_state()

    interval_hours = _parse_int(env_vars.get("TASK_INTERVAL_HOURS", ""), 8)
    raw_tags = env_vars.get("TASK_SELECTED_TAGS")
    legacy_tags = env_vars.get("TASK_SELECTED_CATEGORIES", "")
    selected_tags = _parse_json_list(raw_tags if raw_tags is not None else legacy_tags)
    selected_tags = [str(x) for x in selected_tags]

    llm_configs = [config_to_dict(c) for c in await get_configs(db, "llm")]
    image_configs = [config_to_dict(c) for c in await get_configs(db, "image")]
    search_configs = [config_to_dict(c) for c in await get_configs(db, "search")]
    embedding_configs = [config_to_dict(c) for c in await get_configs(db, "embedding")]

    llm_enabled = await get_enabled_config(db, "llm")
    image_enabled = await get_enabled_config(db, "image")
    search_enabled = await get_enabled_config(db, "search")
    embedding_enabled = await get_enabled_config(db, "embedding")

    search_state = await search_endpoint_status(db)
    pref = preferences.snapshot()
    llm_native_search = False
    llm_native_label = ""
    if llm_enabled:
        from app.services.native_search import capability_available, resolve_spec

        llm_native_search = bool(llm_enabled.api_key_enc) and capability_available(
            llm_enabled.provider,
            llm_enabled.base_url,
            llm_enabled.model_id,
            bool(llm_enabled.native_search),
            llm_enabled.native_search_checked_at,
        )
        if llm_native_search:
            spec = resolve_spec(llm_enabled.provider, llm_enabled.base_url, llm_enabled.model_id)
            llm_native_label = spec.label if spec else (llm_enabled.native_search_kind or "大模型内置搜索")

    return ConfigResponse(
        llm_configs=llm_configs,
        image_configs=image_configs,
        search_configs=search_configs,
        embedding_configs=embedding_configs,
        active_llm=str(llm_enabled.id) if llm_enabled else "",
        active_image=str(image_enabled.id) if image_enabled else "",
        active_search=str(search_enabled.id) if search_enabled else "",
        active_embedding=str(embedding_enabled.id) if embedding_enabled else "",
        nickname=env_vars.get("NICKNAME", ""),
        avatar_url=env_vars.get("AVATAR_URL", ""),
        llm_base_url=llm_enabled.base_url if llm_enabled else "",
        llm_model=llm_enabled.model_id if llm_enabled else "",
        search_base_url=search_enabled.base_url if search_enabled else "",
        has_llm_key=bool(llm_enabled and decrypt_api_key(llm_enabled.api_key_enc)),
        has_search_key=bool(search_enabled and decrypt_api_key(search_enabled.api_key_enc)),
        search_ready=bool(search_state["ready"]),
        search_source=search_state["source"],
        search_label=search_state.get("label", ""),
        llm_native_search=llm_native_search,
        llm_native_search_label=llm_native_label,
        task_interval_hours=interval_hours,
        task_start_hour=_parse_int(env_vars.get("TASK_START_HOUR", ""), 0),
        task_schedule_mode=(
            "smart" if (env_vars.get("TASK_SCHEDULE_MODE", "") or "").strip().lower() == "smart" else "fixed"
        ),
        task_smart_lead_minutes=max(
            5, min(_parse_int(env_vars.get("TASK_SMART_LEAD_MINUTES", ""), 45), 180)
        ),
        task_selected_tags=selected_tags,
        agent_interests_text=pref["interests_text"],
        agent_interests_tags=pref["interests_tags"],
        agent_schedule_text=pref["schedule_text"],
        agent_schedule_plan=pref["schedule_plan"],
        agent_items_per_run=pref["items_per_run"],
        agent_preferences_analyzed_at=pref["analyzed_at"],
        agent_base_prompt=build_base_prompt(interval_hours, selected_tags),
        agent_ext_preset=DEFAULT_EXT_PROMPT,
        agent_ext_mode=env_vars.get("AGENT_EXT_MODE", "preset"),
        agent_ext_prompt=env_vars.get("AGENT_EXT_PROMPT", ""),
        agent_recommend_enabled=env_vars.get("AGENT_RECOMMEND_ENABLED", "") == "true",
        agent_brief_enabled=env_vars.get("AGENT_BRIEF_ENABLED", "") == "true",
        agent_cover_enabled=_cover_mode(env_vars) == "image",
        agent_cover_mode=_cover_mode(env_vars),
        onboarding_done=env_vars.get("ONBOARDING_DONE", "") == "true",
        work_mode=env_vars.get("WORK_MODE", ""),
        db_mode=db_state["mode"],
        db_config=db_state["config"],
        db_complete=db_state["complete"],
    )


@router.put("")
async def update_config(config: ConfigUpdate):
    env_vars = read_env_file()

    # 详细日志：记录收到的配置项（便于排查自动任务设置是否真正生效）
    task_changed = any(v is not None for v in (
        config.task_interval_hours, config.task_start_hour, config.task_selected_tags,
        config.task_schedule_mode, config.task_smart_lead_minutes,
    ))
    if task_changed:
        logger.info(
            "收到自动任务配置保存请求：时间间隔=%s，每日开始=%s，调度方式=%s，智能提前量=%s，标签=%s",
            config.task_interval_hours,
            config.task_start_hour,
            config.task_schedule_mode,
            config.task_smart_lead_minutes,
            (config.task_selected_tags or []) if config.task_selected_tags is not None else "（未修改）",
        )
    if config.work_mode is not None:
        logger.info("收到工作模式修改：%s", config.work_mode)
    if config.agent_brief_enabled is not None or config.agent_recommend_enabled is not None or config.agent_cover_enabled is not None or config.agent_cover_mode is not None:
        logger.info(
            "收到 Agent 开关修改：简报=%s，推荐=%s，封面=%s，封面方式=%s",
            config.agent_brief_enabled, config.agent_recommend_enabled,
            config.agent_cover_enabled, config.agent_cover_mode,
        )

    if config.task_interval_hours is not None:
        env_vars["TASK_INTERVAL_HOURS"] = str(max(1, config.task_interval_hours))
    if config.task_start_hour is not None:
        env_vars["TASK_START_HOUR"] = str(config.task_start_hour % 24)
    if config.task_schedule_mode is not None:
        mode = (config.task_schedule_mode or "").strip().lower()
        if mode not in {"fixed", "smart"}:
            raise HTTPException(status_code=400, detail="不支持的调度方式")
        env_vars["TASK_SCHEDULE_MODE"] = mode
    if config.task_smart_lead_minutes is not None:
        env_vars["TASK_SMART_LEAD_MINUTES"] = str(max(5, min(int(config.task_smart_lead_minutes), 180)))
    if config.task_selected_tags is not None:
        env_vars["TASK_SELECTED_TAGS"] = json.dumps(config.task_selected_tags, ensure_ascii=False)
    if config.agent_interests_text is not None:
        env_vars[preferences.INTERESTS_TEXT_ENV] = " ".join(config.agent_interests_text.split())[:2000]
    if config.agent_schedule_text is not None:
        env_vars[preferences.SCHEDULE_TEXT_ENV] = " ".join(config.agent_schedule_text.split())[:2000]

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
    if config.agent_cover_mode is not None:
        mode = (config.agent_cover_mode or "").strip().lower()
        if mode not in {"image", "svg", "off"}:
            raise HTTPException(status_code=400, detail="不支持的封面生成方式")
        env_vars["AGENT_COVER_MODE"] = mode
        # 同步旧开关，避免其它读取方（如停用文生图模型的校验）状态不一致
        env_vars["AGENT_COVER_ENABLED"] = "true" if mode == "image" else "false"
    elif config.agent_cover_enabled is not None:
        env_vars["AGENT_COVER_MODE"] = "image" if config.agent_cover_enabled else "off"
    if config.onboarding_done is not None:
        env_vars["ONBOARDING_DONE"] = "true" if config.onboarding_done else "false"
    if config.work_mode is not None:
        work_mode = (config.work_mode or "").strip().lower()
        if work_mode not in {"assist", "autonomous", ""}:
            raise HTTPException(status_code=400, detail="工作模式只能是 assist（辅助）或 autonomous（自主）")
        env_vars["WORK_MODE"] = work_mode

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

    write_env_file(env_vars)

    if task_changed:
        logger.info(
            "自动任务配置已保存生效：间隔=%s 小时，每日开始=%s 点，调度方式=%s，标签=%s",
            env_vars.get("TASK_INTERVAL_HOURS"),
            env_vars.get("TASK_START_HOUR"),
            env_vars.get("TASK_SCHEDULE_MODE", "fixed"),
            env_vars.get("TASK_SELECTED_TAGS"),
        )

    return {"message": "ok"}


class PreferencesAnalyzeBody(BaseModel):
    interests_text: str = ""
    schedule_text: str = ""
    mode: str = "smart"


@router.post("/preferences/analyze")
async def analyze_preferences(body: PreferencesAnalyzeBody, db: AsyncSession = Depends(get_db)):
    """把自然语言兴趣/时间描述解析成标签、排程与每次采集量并写回 .env。"""
    if assist_service.is_assist():
        raise HTTPException(
            status_code=409,
            detail="辅助模式下平台不调用模型；兴趣与节奏请由外部 Agent 访谈后写入 /api/assist/onboarding。",
        )
    if not body.interests_text.strip() and not body.schedule_text.strip():
        raise HTTPException(status_code=400, detail="请至少描述兴趣或时间偏好")
    result = await preferences.analyze_and_save(
        db,
        body.interests_text,
        body.schedule_text,
        body.mode,
    )
    # 智能调度的画像缓存需要失效，否则要等 10 分钟才用上新的时段描述
    try:
        from app.services.scheduler import scheduler

        scheduler.invalidate_smart_profile()
    except Exception:
        pass
    return result


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
