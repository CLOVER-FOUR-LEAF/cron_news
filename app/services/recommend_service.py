import json
import re
from typing import Any, Callable, Awaitable

import httpx
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.news import News
from app.services.agent_prompts import get_agent_prompt
from app.services.model_configs import get_active_endpoint

EmitFn = Callable[..., Awaitable[None]]

CANDIDATE_POOL = 200
BATCH_SIZE = 20
RELATED_LIMIT = 6


async def _ask_llm(db, system_prompt: str, prompt: str) -> str:
    endpoint = await get_active_endpoint(db, "llm")
    if not endpoint or not endpoint["api_key"] or not endpoint["model_id"]:
        raise ValueError("大语言模型未配置")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{endpoint['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {endpoint['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": endpoint["model_id"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def _parse_ids(text: str, valid_ids: set[int], exclude_id: int) -> list[int]:
    nums = re.findall(r"\d+", text or "")
    result = []
    for n in nums:
        try:
            i = int(n)
        except ValueError:
            continue
        if i in valid_ids and i != exclude_id and i not in result:
            result.append(i)
        if len(result) >= RELATED_LIMIT:
            break
    return result


async def run_recommend_task(db: AsyncSession, emit: EmitFn | None = None) -> dict[str, Any]:
    async def _emit(event_type: str, text: str, **extra: Any):
        if emit:
            await emit(event_type, text, **extra)

    if not await get_active_endpoint(db, "llm"):
        await _emit("error", "大语言模型未配置，无法执行智能推荐")
        return {"updated": 0}

    system_prompt = await get_agent_prompt(db, "recommend")

    await _emit("thinking", "智能推荐 Agent 启动，分析新闻相关性…")

    pool_result = await db.execute(
        select(News).where(News.is_deleted == 0).order_by(News.collected_at.desc()).limit(CANDIDATE_POOL)
    )
    pool = pool_result.scalars().all()

    if len(pool) < 2:
        await _emit("info", "新闻数量不足，跳过推荐计算")
        return {"updated": 0}

    valid_ids = {n.id for n in pool}
    catalog_lines = [f"[{n.id}] ({n.category_name or '未分类'}) {n.title}" for n in pool]
    catalog = "\n".join(catalog_lines)

    target_result = await db.execute(
        select(News)
        .where(News.is_deleted == 0, or_(News.related_ids.is_(None), News.related_ids == ""))
        .order_by(News.collected_at.desc())
        .limit(BATCH_SIZE)
    )
    targets = target_result.scalars().all()

    if not targets:
        await _emit("info", "所有新闻均已计算推荐，本次无需更新")
        return {"updated": 0}

    await _emit("info", f"候选新闻池 {len(pool)} 条，待计算 {len(targets)} 条")

    updated = 0
    for news in targets:
        summary = (news.summary or news.title)[:120]
        prompt = f"""下面是新闻候选列表，每行格式为 [ID] (分类) 标题：
{catalog}

请为以下新闻找出内容最相关的至多 {RELATED_LIMIT} 条新闻（可跨分类，依据主题、事件、人物的关联性判断）：
目标新闻 [{news.id}] ({news.category_name or '未分类'}) {news.title}
摘要：{summary}

只输出相关新闻的 ID，用逗号分隔，不要输出任何其他文字。"""

        await _emit("tool", f'llm.relate([{news.id}]) "{news.title[:24]}…"')
        try:
            answer = await _ask_llm(db, system_prompt, prompt)
            related = _parse_ids(answer, valid_ids, news.id)
            news.related_ids = json.dumps(related)
            updated += 1
            await _emit("save", f"✓ [{news.id}] 关联 {len(related)} 条 → {related}")
        except Exception as e:
            await _emit("error", f"[{news.id}] 推荐计算失败: {e}")

    await db.flush()
    await _emit("success", f"智能推荐完成，更新 {updated} 条新闻的相关性", updated=updated)
    return {"updated": updated}
