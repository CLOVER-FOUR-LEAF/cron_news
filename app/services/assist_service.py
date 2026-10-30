"""辅助模式：外部 Agent 的初始化状态、协作约定与运行快照。

设计原则：
- 辅助模式下平台不调用任何模型，只负责接收、存储与展示外部 Agent 写入的数据；
- 初始化内容（用户画像、兴趣、节奏、口味）由外部 Agent 访谈后提交，平台只做安全落库；
- 状态文件 `memory/ASSIST.json` 供外部 Agent 跨会话恢复，与人类可读的 `PROFILE.md` 互补。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import news as news_crud
from app.env_store import read_env_file, write_env_file
from app.logging_setup import get_logger
from app.models import News, NewsTag
from app.services import brief_service, feedback_service, memory_service, tag_service
from app.services.scheduler import scheduler

logger = get_logger("assist")

# 与 skill/SKILL.md 的 frontmatter / manifest 保持一致；修改 Skill 时同步递增
SKILL_VERSION = "1.0.0"
STATE_VERSION = 1
STATE_FILENAME = "ASSIST.json"
ONBOARDING_SECTIONS = ("profile", "interests", "watch", "preferences", "avoid", "notes")
_DEFAULT_NICKNAME_RE = re.compile(r"^User\d{6}$")


def work_mode() -> str:
    return (read_env_file().get("WORK_MODE", "") or "").strip().lower()


def is_assist() -> bool:
    return work_mode() == "assist"


def state_path() -> Path:
    return memory_service.memory_dir() / STATE_FILENAME


def read_state() -> dict[str, Any]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["version"] = STATE_VERSION
    payload["skill_version"] = SKILL_VERSION
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return payload


def reset_onboarding_state() -> dict[str, Any]:
    """只清除外部 Agent 的初始化状态，不动 PROFILE.md 与关注标签。"""
    path = state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f"无法删除初始化状态：{exc}") from exc
    return {"ok": True, "reset": True, "path": str(path)}


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


async def apply_onboarding(
    db: AsyncSession,
    *,
    sections: dict[str, str],
    tags: list[str] | None = None,
    nickname: str = "",
    agent: str = "",
    schedule: dict[str, Any] | None = None,
    editorial: dict[str, Any] | None = None,
    replace_sections: bool = False,
) -> dict[str, Any]:
    """把外部 Agent 访谈得到的初始化资料写入平台。

    - 只允许写固定的记忆段落，保留用户手写内容与自动画像块；
    - 标签为增量关注，不会取消用户已有标签；
    - 未提供的 tags / schedule / editorial 会保留上一次的状态；
    - 默认不覆盖已有自定义昵称；
    - 写入 `memory/ASSIST.json` 作为外部 Agent 的跨会话状态。
    """
    mode = work_mode()
    if mode != "assist":
        raise ValueError("not_assist")

    now = datetime.now()
    existing = read_state()
    memory_saved = True
    memory_error = ""
    cleaned_sections = {
        key: str(body or "").strip()[:8000]
        for key, body in (sections or {}).items()
        if key in ONBOARDING_SECTIONS and str(body or "").strip()
    }

    if memory_service.enabled():
        try:
            text = await memory_service.read_memory()
            for key, body in cleaned_sections.items():
                text = memory_service.write_section(
                    text, key, body, replace=replace_sections
                )
            marker = (
                f"- [{now:%Y-%m-%d}] 外部 Agent 初始化完成"
                f"（{_clean_text(agent, 80) or '外部 Agent'}，skill v{SKILL_VERSION}）"
            )
            text = memory_service.write_section(text, "notes", marker, replace=False)
            await memory_service.write_memory(text)
        except Exception as exc:  # noqa: BLE001 - 初始化不应因记忆目录异常而全部失败
            memory_saved = False
            memory_error = str(exc)
            logger.warning("辅助模式初始化写入记忆失败：%s", exc)
    else:
        memory_saved = False
        memory_error = "记忆系统未启用（AGENT_MEMORY_ENABLED=false）"

    applied_tags: list[str] | None = None
    if tags:
        applied_tags = []
        for raw in tags[:20]:
            try:
                tag, _ = await tag_service.set_follow(db, str(raw or ""), True)
            except ValueError:
                continue
            if tag and tag not in applied_tags:
                applied_tags.append(tag)

    env = read_env_file()
    nickname_saved = ""
    requested_nickname = _clean_text(nickname, 20)
    current_nickname = (env.get("NICKNAME") or "").strip()
    if requested_nickname and (
        not current_nickname or _DEFAULT_NICKNAME_RE.fullmatch(current_nickname)
    ):
        nickname_saved = requested_nickname
        env["NICKNAME"] = nickname_saved
    env["ONBOARDING_DONE"] = "true"
    write_env_file(env)
    await db.commit()

    state = write_state(
        {
            "onboarded_at": now.isoformat(timespec="seconds"),
            "agent": _clean_text(agent, 80) or existing.get("agent", ""),
            "nickname": nickname_saved or (env.get("NICKNAME") or ""),
            "tags": (
                applied_tags
                if applied_tags is not None
                else existing.get("tags", [])
            ),
            "schedule": schedule if schedule is not None else existing.get("schedule", {}),
            "editorial": editorial if editorial is not None else existing.get("editorial", {}),
            "memory_saved": memory_saved,
        }
    )
    final_tags = state.get("tags") or []
    logger.info(
        "辅助模式初始化完成：%s 个标签，记忆写入=%s，Agent=%s",
        len(final_tags),
        memory_saved,
        _clean_text(agent, 80) or "未注明",
    )
    return {
        "ok": True,
        "work_mode": mode,
        "onboarded_at": state["onboarded_at"],
        "tags": final_tags,
        "nickname": state["nickname"],
        "memory_saved": memory_saved,
        "memory_error": memory_error,
        "state": state,
    }


async def _batch_snapshot(db: AsyncSession) -> dict[str, Any]:
    batch_id = await tag_service.latest_batch_id(db)
    if batch_id:
        count = int(
            await db.scalar(
                select(func.count()).where(
                    News.is_deleted == 0, News.batch_id == batch_id
                )
            )
            or 0
        )
        return {"id": batch_id, "count": count, "fallback": ""}
    since = datetime.now() - timedelta(hours=24)
    count = int(
        await db.scalar(
            select(func.count()).where(
                News.is_deleted == 0, News.collected_at >= since
            )
        )
        or 0
    )
    return {"id": "", "count": count, "fallback": "last_24h"}


async def _light_stats(db: AsyncSession) -> dict[str, int]:
    """状态接口只需要少量计数，避免每次轮询都跑完整的 30 天统计。"""
    today_start = datetime.combine(date.today(), datetime.min.time())
    total = int(
        await db.scalar(select(func.count()).where(News.is_deleted == 0)) or 0
    )
    unread = int(
        await db.scalar(
            select(func.count()).where(News.is_deleted == 0, News.is_read == 0)
        )
        or 0
    )
    today = int(
        await db.scalar(
            select(func.count()).where(
                News.is_deleted == 0, News.collected_at >= today_start
            )
        )
        or 0
    )
    tagged = int(
        await db.scalar(
            select(func.count(func.distinct(NewsTag.news_id)))
            .join(News, News.id == NewsTag.news_id)
            .where(News.is_deleted == 0)
        )
        or 0
    )
    return {
        "total": total,
        "read": total - unread,
        "unread": unread,
        "today": today,
        "tagged": tagged,
        "untagged": max(0, total - tagged),
    }


async def status_snapshot(db: AsyncSession) -> dict[str, Any]:
    """外部 Agent 的一站式健康检查 / 状态恢复快照。"""
    state = read_state()
    stats = await _light_stats(db)
    counts = await news_crud.get_my_counts(db)
    followed = await tag_service.followed_tags(db)
    top_tags = await tag_service.top_tags(db, days=30, limit=12)
    batch = await _batch_snapshot(db)

    latest_news = (
        await db.execute(
            select(News)
            .where(News.is_deleted == 0)
            .order_by(News.collected_at.desc())
            .limit(1)
        )
    ).scalars().first()
    latest_payload = None
    if latest_news:
        latest_payload = {
            "id": latest_news.id,
            "title": latest_news.title,
            "source": latest_news.source,
            "collected_at": (
                latest_news.collected_at.isoformat()
                if latest_news.collected_at
                else None
            ),
            "batch_id": latest_news.batch_id,
        }

    brief = await brief_service.get_daily_brief(db, None)
    brief_payload = {
        "exists": bool(brief),
        "date": brief.brief_date if brief else "",
        "source": brief.source if brief else "",
        "updated_at": (
            brief.updated_at.isoformat() if brief and brief.updated_at else None
        ),
    }

    feedback = await feedback_service.list_feedback(db, limit=5)
    memory_status = memory_service.status()
    env = read_env_file()
    sched = scheduler.status

    return {
        "work_mode": work_mode(),
        "is_assist": is_assist(),
        "skill_version": SKILL_VERSION,
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "onboarded": bool(state.get("onboarded_at")),
        "onboarding": state,
        "nickname": env.get("NICKNAME", ""),
        "stats": {
            "total": stats.get("total", 0),
            "today": stats.get("today", 0),
            "read": stats.get("read", 0),
            "unread": stats.get("unread", 0),
            "fav": counts.get("fav_count", 0),
            "later": counts.get("later_count", 0),
            "tagged": stats.get("tagged", 0),
            "untagged": stats.get("untagged", 0),
        },
        "latest_batch": batch,
        "latest_news": latest_payload,
        "tags": {
            "followed": followed,
            "top": top_tags,
        },
        "brief": brief_payload,
        "feedback": {
            "pending": feedback.get("pending", 0),
            "items": feedback.get("items") or [],
        },
        "memory": {
            "enabled": memory_status.get("enabled", False),
            "exists": memory_status.get("exists", False),
            "updated_at": memory_status.get("updated_at"),
            "path": memory_status.get("path", ""),
        },
        "scheduler": {
            "running": bool(sched.get("running")),
            # 辅助模式不调度；避免切换后 15 秒内展示旧的 next_run
            "next_run": sched.get("next_run") if work_mode() == "autonomous" else None,
            "last_skip": sched.get("last_skip"),
        },
        "note": (
            "辅助模式下平台不主动采集；请由外部 Agent 按 /api/assist/status 中的协作状态继续。"
            if work_mode() == "assist"
            else "当前不是辅助模式，外部 Agent 初始化会被拒绝。请先在设置中切换为辅助模式。"
        ),
    }
