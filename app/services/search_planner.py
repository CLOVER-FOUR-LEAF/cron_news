"""搜索策略规划 Agent。

围绕用户关注的标签生成细分查询，另外保留一条泛热点查询，避免信息茧房。
新闻入库时不限制在搜索标签内，分析 Agent 会自由打多个标签。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import News
from app.services import llm_client, memory_service, preferences, tag_service
from app.services.json_utils import extract_json_object
from app.services.model_configs import get_active_endpoint

logger = get_logger("search_planner")


def _env_bool(name: str, default: bool) -> bool:
    raw = (read_env_file().get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(read_env_file().get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def extract_json(text: str) -> dict[str, Any] | None:
    return extract_json_object(text)


async def _recent_titles(db: AsyncSession, limit: int = 160) -> list[str]:
    since = datetime.now() - timedelta(days=14)
    rows = (
        await db.execute(
            select(News.title)
            .where(News.is_deleted == 0, News.collected_at >= since)
            .order_by(News.collected_at.desc())
            .limit(limit)
        )
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def _heuristic_queries(tags: list[str], interval_hours: int) -> dict[str, list[str]]:
    planned: dict[str, list[str]] = {}
    for tag in tags[:8]:
        planned[tag] = [f"{tag} 最新进展"]
    planned["_general"] = ["今日重大新闻", "国际 财经 科技 要闻"]
    return planned


async def plan_tag_queries(
    db: AsyncSession,
    tags: list[str],
    interval_hours: int,
) -> dict[str, list[str]]:
    """返回 {标签或 _general: [查询词...]}。任何失败都回退到启发式。"""
    tags = tag_service.normalize_tags(tags)[:8]
    per_tag = _env_int("AGENT_COLLECT_QUERIES_PER_TAG", 2, 1, 3)
    heuristic = _heuristic_queries(tags, interval_hours)
    if not _env_bool("AGENT_QUERY_PLANNER", True):
        return heuristic
    if not await get_active_endpoint(db, "llm"):
        return heuristic

    memory_ctx = await memory_service.build_prompt_context(db, "collect")
    interests_ctx = preferences.interests_text()
    recent = await _recent_titles(db)
    recent_text = "\n".join(f"- {title}" for title in recent[:80]) or "（近期无收录）"
    tags_text = "、".join(tags) if tags else "（未设置关注标签）"

    system_prompt = (
        "你是资讯采集策略规划师。用户关注一组标签，你要为每个标签生成互补的搜索查询词，"
        "并额外生成泛热点查询，避免只看到关注领域。\n"
        "要求：\n"
        "1. 必须结合用户记忆中的跟踪任务、兴趣、用户兴趣描述与「需要避免」。\n"
        "2. 查询词要指向具体事件、公司、产品、人物或政策，例如「英伟达 财报」「OpenAI 新模型发布」；"
        "不要写「科技新闻」「财经新闻」「今日要闻」这类会搜到门户首页的泛词。\n"
        "3. 同一标签覆盖不同细分方向，优先近期未覆盖的角度与最新进展。\n"
        "4. 查询词简短（2-18 字），不要写「最近 X 小时」「最新」这类时效词，系统会统一加时间窗。\n"
        "5. 新闻入库时会自由打标签，因此查询词不要试图穷举所有标签。\n"
        "6. JSON 的键必须与关注标签完全一致，不能遗漏任何一个标签。\n"
        "7. 只输出 JSON，不要 Markdown 代码块或解释：\n"
        '   {"标签名":["查询1","查询2"], ... , "_general":["泛热点查询"]}'
    )
    user_prompt = (
        (memory_ctx + "\n\n" if memory_ctx else "")
        + (f"用户兴趣描述：{interests_ctx}\n\n" if interests_ctx else "")
        + f"关注标签：{tags_text}\n\n"
        + "近期已收录标题（避免重复角度）：\n"
        + recent_text
        + "\n\n请为每个关注标签生成 1-2 条查询，并给出 _general 泛热点查询。"
    )

    try:
        answer = await llm_client.chat_text(
            db,
            system_prompt,
            user_prompt,
            purpose="search_plan",
            temperature=0.4,
            max_tokens=800,
            timeout=60.0,
        )
        payload = extract_json(answer) or {}
    except Exception as exc:
        logger.warning("搜索规划失败，使用启发式：%s", exc)
        return heuristic

    # 模型偶尔会漏掉某个标签；单独补一次，避免退回「科技 最新进展」这类会被门户页淹没的泛词。
    missing = [tag for tag in tags if not payload.get(tag)]
    if missing:
        try:
            fix_prompt = (
                "上一次输出遗漏了这些关注标签："
                + "、".join(missing)
                + "。请只为它们各生成 1-2 条具体查询，指向具体事件、公司、产品或政策，"
                "不要写「XX新闻」「XX最新进展」这类泛词。"
                '只输出 JSON，例如 {"科技":["AI芯片 新品发布","大模型 融资"]}，不要其它文字。'
            )
            fix_answer = await llm_client.chat_text(
                db,
                system_prompt,
                fix_prompt,
                purpose="search_plan",
                temperature=0.4,
                max_tokens=400,
                timeout=45.0,
            )
            payload.update(extract_json(fix_answer) or {})
        except Exception as exc:
            logger.warning("补齐缺失标签的搜索查询失败：%s", exc)

    planned: dict[str, list[str]] = {}
    for tag in tags:
        raw = payload.get(tag)
        if isinstance(raw, str):
            raw = [raw]
        queries = []
        if isinstance(raw, list):
            for item in raw:
                query = re.sub(r"\s+", " ", str(item or "")).strip()
                if query and query not in queries:
                    queries.append(query[:60])
                if len(queries) >= per_tag:
                    break
        planned[tag] = queries or heuristic[tag]

    general = payload.get("_general")
    if isinstance(general, str):
        general = [general]
    general_queries = []
    if isinstance(general, list):
        for item in general:
            query = re.sub(r"\s+", " ", str(item or "")).strip()
            if query and query not in general_queries:
                general_queries.append(query[:60])
            if len(general_queries) >= 2:
                break
    planned["_general"] = general_queries or heuristic["_general"]

    logger.info("搜索规划完成：%s", planned)
    return planned
