from datetime import datetime, date
from typing import Any, Callable, Awaitable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import News, Category, Brief
from app.services.agent_prompts import get_agent_prompt
from app.services.model_configs import get_active_endpoint

EmitFn = Callable[..., Awaitable[None]]


async def _llm_endpoint(db) -> dict | None:
    return await get_active_endpoint(db, "llm")


async def _ask_llm(db, system_prompt: str, prompt: str) -> str:
    endpoint = await get_active_endpoint(db, "llm")
    if not endpoint or not endpoint["api_key"] or not endpoint["model_id"]:
        raise ValueError("大语言模型未配置")
    async with httpx.AsyncClient(timeout=120.0) as client:
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
                "temperature": 0.5,
                "max_tokens": 1200,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def run_brief_task(db: AsyncSession, emit: EmitFn | None = None) -> dict[str, Any]:
    async def _emit(event_type: str, text: str, **extra: Any):
        if emit:
            await emit(event_type, text, **extra)

    if not await _llm_endpoint(db):
        await _emit("error", "大语言模型未配置，无法生成每日简报")
        return {"generated": 0}

    system_prompt = await get_agent_prompt(db, "brief")

    await _emit("thinking", "每日简报 Agent 启动，分析今日各分类新闻…")

    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())

    result = await db.execute(select(Category).order_by(Category.sort_order.desc(), Category.id))
    categories = result.scalars().all()

    generated = 0
    for cat in categories:
        result = await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.category_id == cat.id, News.collected_at >= today_start)
            .order_by(News.collected_at.desc())
            .limit(30)
        )
        items = result.scalars().all()
        if not items:
            continue

        news_lines = "\n".join(
            f"- {n.title}：{(n.summary or '').strip()[:80]}" for n in items
        )
        prompt = f"""今天是 {today.isoformat()}。以下是「{cat.name}」分类今日收录的 {len(items)} 条新闻：
{news_lines}

请撰写该分类的每日简报，要求：
1. 使用 Markdown 格式，以二级标题「{cat.name} · 每日简报」开头；
2. 先给出一段总体概述（2-3 句）；
3. 挑选最重要的若干条逐条点评（加粗标题 + 1-2 句点评）；
4. 结尾给出一句展望或提示；
5. 全文控制在 400-800 字，语言精炼、客观。"""

        await _emit("tool", f'llm.brief("{cat.name}") {len(items)} 条新闻')
        try:
            content = await _ask_llm(db, system_prompt, prompt)
        except Exception as e:
            await _emit("error", f"[{cat.name}] 简报生成失败: {e}")
            continue

        existing = await db.execute(
            select(Brief).where(Brief.category_name == cat.name, Brief.brief_date == today.isoformat())
        )
        for old in existing.scalars().all():
            await db.delete(old)
        db.add(Brief(category_name=cat.name, brief_date=today.isoformat(), content=content, source="自主"))
        generated += 1
        await _emit("save", f"✓ [{cat.name}] 简报已生成")

    await db.flush()
    await _emit("success", f"每日简报完成，生成 {generated} 个分类", generated=generated)
    return {"generated": generated}


async def get_brief_dates(db: AsyncSession, category_name: str) -> list[str]:
    result = await db.execute(
        select(Brief.brief_date)
        .where(Brief.category_name == category_name)
        .order_by(Brief.brief_date.desc())
    )
    return [row[0] for row in result.all()]


async def get_brief_by_date(db: AsyncSession, category_name: str, brief_date: str) -> Brief | None:
    result = await db.execute(
        select(Brief)
        .where(Brief.category_name == category_name, Brief.brief_date == brief_date)
        .order_by(Brief.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_latest_brief(db: AsyncSession, category_name: str) -> Brief | None:
    result = await db.execute(
        select(Brief)
        .where(Brief.category_name == category_name)
        .order_by(Brief.brief_date.desc(), Brief.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()
