import json
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.news import News
from app.models.category import Category
from app.crud.category import get_default_category
from app.env_store import read_env_file, write_env_file
from app.services.agent_prompts import get_agent_prompt

EmitFn = Callable[..., Awaitable[None]]


def _timed_system_prompt() -> str:
    return get_agent_prompt("timed")


async def search_news(query: str, max_results: int = 10, hours: int | None = None) -> list[dict[str, Any]]:
    if not settings.SEARCH_BASE_URL or not settings.SEARCH_API_KEY:
        raise ValueError("搜索服务未配置")

    payload: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
    }

    if hours:
        now = datetime.now()
        payload["hours"] = hours
        payload["from"] = (now - timedelta(hours=hours)).isoformat()
        payload["to"] = now.isoformat()

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{settings.SEARCH_BASE_URL}/search",
            headers={
                "Authorization": f"Bearer {settings.SEARCH_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("results", [])


async def generate_news_content(title: str, summary: str) -> str:
    if not settings.LLM_BASE_URL or not settings.LLM_API_KEY or not settings.LLM_MODEL:
        return summary or ""

    prompt = f"""请根据以下新闻标题和摘要，生成一篇完整的Markdown格式新闻正文。
要求：
1. 内容详实，逻辑清晰
2. 使用Markdown格式（标题、段落、列表等）
3. 约500-800字
4. 语言风格为新闻报道风格

标题：{title}
摘要：{summary}

请直接输出Markdown内容，不要添加其他说明。"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _timed_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 2000,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def _short(text: str, limit: int = 36) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def process_search_results(
    db: AsyncSession,
    category_name: str,
    search_results: list[dict[str, Any]],
    emit: EmitFn | None = None,
) -> int:
    async def _emit(event_type: str, text: str, **extra: Any):
        if emit:
            await emit(event_type, text, **extra)

    category = await db.execute(
        Category.__table__.select().where(Category.name == category_name)
    )
    category = category.fetchone()

    if not category:
        default = await get_default_category(db)
        category_id = default.id if default else 1
    else:
        category_id = category.id

    llm_ready = bool(settings.LLM_BASE_URL and settings.LLM_API_KEY and settings.LLM_MODEL)

    count = 0
    skipped = 0
    for result in search_results:
        title = result.get("title", "").strip()
        if not title:
            continue

        existing = await db.execute(
            News.__table__.select().where(News.title == title)
        )
        if existing.fetchone():
            skipped += 1
            continue

        summary = result.get("snippet", result.get("description", ""))
        url = result.get("url", result.get("link", ""))
        source = result.get("source", result.get("domain", ""))
        published = result.get("published_date", result.get("date", None))

        collected_at = datetime.now()
        if published:
            try:
                collected_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        content = ""
        if llm_ready:
            await _emit("tool", f'llm.generate("{_short(title)}") → 生成正文')
            try:
                content = await generate_news_content(title, summary)
            except Exception as e:
                await _emit("error", f"正文生成失败，回退为摘要: {e}")
                content = summary or ""
        else:
            content = summary or ""

        news = News(
            title=title,
            summary=summary[:500] if summary else None,
            content=content,
            source_url=url[:500] if url else None,
            source=source[:100] if source else None,
            category_id=category_id,
            collected_at=collected_at,
        )
        db.add(news)
        count += 1
        await _emit("save", f"✓ {_short(title, 42)}")

    if skipped:
        await _emit("info", f"过滤重复新闻 {skipped} 条")

    await db.flush()
    return count


async def run_search_task(db: AsyncSession, emit: EmitFn | None = None) -> dict[str, Any]:
    async def _emit(event_type: str, text: str, **extra: Any):
        if emit:
            await emit(event_type, text, **extra)

    env_vars = read_env_file()

    try:
        interval_hours = max(1, int(env_vars.get("TASK_INTERVAL_HOURS", "8")))
    except (ValueError, TypeError):
        interval_hours = 6

    mode = env_vars.get("TASK_MODE", "preset")

    selected: list[str] = []
    raw_selected = env_vars.get("TASK_SELECTED_CATEGORIES", "")
    if raw_selected:
        try:
            parsed = json.loads(raw_selected)
            if isinstance(parsed, list):
                selected = [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass

    await _emit("thinking", "初始化采集任务，读取任务配置…")
    await _emit(
        "info",
        f"触发模式: {'预设' if mode == 'preset' else '自定义'} · 时间窗口: 过去 {interval_hours} 小时",
    )

    result = await db.execute(
        select(Category).where(Category.is_default == False).order_by(Category.sort_order.desc(), Category.id)
    )
    categories = result.scalars().all()

    if selected:
        categories = [c for c in categories if c.name in selected]

    if not categories:
        await _emit("error", "没有可采集的分类，任务终止")
        return {"total_new": 0, "details": {}, "interval_hours": interval_hours}

    await _emit("info", f"目标分类({len(categories)}): {'、'.join(c.name for c in categories)}")

    total_count = 0
    results = {}

    for cat in categories:
        query = f"{cat.name}新闻 最近{interval_hours}小时"
        await _emit("thinking", f"分析「{cat.name}」领域最近 {interval_hours} 小时的热点…")
        await _emit("tool", f'search("{query}") → 调用搜索服务')
        try:
            search_results = await search_news(query, max_results=15, hours=interval_hours)
        except Exception as e:
            await _emit("error", f"「{cat.name}」搜索失败: {e}")
            results[cat.name] = f"错误: {str(e)}"
            continue

        await _emit("result", f"「{cat.name}」获取 {len(search_results)} 条候选结果")
        count = await process_search_results(db, cat.name, search_results, emit)
        results[cat.name] = count
        total_count += count
        await _emit("result", f"「{cat.name}」入库完成，新增 {count} 条")

    env_vars["LAST_SEARCH_TIME"] = datetime.now().isoformat()
    write_env_file(env_vars)

    await _emit("success", f"采集任务完成，共新增 {total_count} 条新闻", total_new=total_count)

    return {
        "total_new": total_count,
        "details": results,
        "interval_hours": interval_hours,
        "search_time": env_vars["LAST_SEARCH_TIME"],
    }
