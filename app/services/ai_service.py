import asyncio
import json
from datetime import datetime
from typing import Any, Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_session
from app.models.news import News
from app.models.category import Category
from app.crud.category import get_default_category
from app.env_store import read_env_file, write_env_file
from app.logging_setup import get_logger
from app.services.agent_prompts import get_agent_prompt
from app.services.cover_service import (
    COVER_MODE_OFF,
    assign_news_cover,
    cover_mode,
)
from app.services.llm_client import chat_text
from app.services.model_configs import get_active_endpoint
from app.services.search_providers import run_search

logger = get_logger("ai_service")

EmitFn = Callable[..., Awaitable[None]]

COVER_DIR = BASE_DIR / "images" / "cover"
DEFAULT_COVER_DIR = COVER_DIR / "default"

# 正文生成的并发数：大模型单次调用较慢（实测可达数十秒），并发可显著缩短整轮采集耗时
CONTENT_CONCURRENCY = 4
# 封面生成的并发数（SVG 也要调模型，控制并发避免拖慢整轮）
COVER_CONCURRENCY = 3


async def search_news(db: AsyncSession, query: str, max_results: int = 10, hours: int | None = None) -> list[dict[str, Any]]:
    endpoint = await get_active_endpoint(db, "search")
    if not endpoint or not endpoint["api_key"]:
        raise ValueError("搜索服务未配置")

    return await run_search(
        provider=endpoint.get("provider", ""),
        base_url=endpoint["base_url"],
        api_key=endpoint["api_key"],
        query=query,
        max_results=max_results,
        hours=hours,
    )


async def generate_news_content(db, title: str, summary: str) -> str:
    endpoint = await get_active_endpoint(db, "llm")
    if not endpoint or not endpoint["api_key"] or not endpoint["model_id"]:
        return summary or ""

    system_prompt = await get_agent_prompt(db, "timed")

    prompt = f"""请根据以下新闻标题和摘要，用简体中文撰写一篇完整的新闻正文。

要求：
1. 直接输出新闻正文本身，作为最终展示内容
2. 使用 Markdown 格式（标题、段落、列表等）
3. 约 500-800 字，专业新闻报道风格，客观中立
4. 不要输出思考过程、任务说明、约束规则或任何解释性文字
5. 不要用 Markdown 代码块包裹正文

标题：{title}
摘要：{summary}

请直接输出正文。"""

    return await chat_text(
        db,
        system_prompt,
        prompt,
        purpose="article",
        temperature=0.7,
        max_tokens=2000,
        timeout=60.0,
    )


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

    llm_endpoint = await get_active_endpoint(db, "llm")
    llm_ready = bool(llm_endpoint and llm_endpoint["api_key"] and llm_endpoint["model_id"])
    mode = cover_mode()
    can_cover = mode != COVER_MODE_OFF

    count = 0
    skipped = 0

    # 阶段1：去重并收集候选（不调用大模型）
    candidates: list[dict[str, Any]] = []
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

        candidates.append({
            "title": title,
            "summary": summary,
            "url": url,
            "source": source,
            "collected_at": collected_at,
        })

    # 阶段2：并行生成正文（限制并发，避免打爆大模型 API）
    # 每个任务使用独立 session：AsyncSession 不支持并发复用，且 LLM 调用期间不应持有主事务
    logger.info("分类「%s」候选 %s 条，开始并行生成正文（并发 %s）", category_name, len(candidates), CONTENT_CONCURRENCY)
    if llm_ready and candidates:
        sem = asyncio.Semaphore(CONTENT_CONCURRENCY)

        async def gen(cand: dict) -> str:
            title = cand["title"]
            summary = cand["summary"]
            async with sem:
                await _emit("tool", f'llm.generate("{_short(title)}") → 生成正文')
                try:
                    async with get_session() as gen_db:
                        return await generate_news_content(gen_db, title, summary)
                except Exception as e:
                    await _emit("error", f"正文生成失败，回退为摘要: {e}")
                    return summary or ""

        contents: list[str] = await asyncio.gather(*[gen(c) for c in candidates])
    else:
        contents = [(c["summary"] or "") for c in candidates]

    # 阶段3：批量入库，先提交释放写锁，再生成封面（封面调用可能很慢）
    inserted: list[News] = []
    for cand, content in zip(candidates, contents):
        news = News(
            title=cand["title"],
            summary=(cand["summary"] or "")[:500] if cand["summary"] else None,
            content=content,
            source_url=(cand["url"] or "")[:500] if cand["url"] else None,
            source=(cand["source"] or "")[:100] if cand["source"] else None,
            category_id=category_id,
            collected_at=cand["collected_at"],
        )
        db.add(news)
        inserted.append(news)

    if inserted:
        await db.flush()
        await db.commit()

    if can_cover and inserted:
        # 封面调用较慢：并发处理；每个任务独立 session，避免互相 expire / rollback 影响
        sem = asyncio.Semaphore(COVER_CONCURRENCY)
        targets = [(news.id, news.title) for news in inserted]

        async def make_cover(news_id: int, title: str):
            async with sem:
                try:
                    async with get_session() as cover_db:
                        result = await cover_db.execute(
                            select(News).where(News.id == news_id)
                        )
                        news = result.scalars().first()
                        if not news:
                            return
                        await assign_news_cover(cover_db, news, emit)
                        await cover_db.commit()
                except Exception as e:
                    await _emit("error", f"封面处理失败（{_short(title, 20)}）: {e}")

        await asyncio.gather(*[make_cover(news_id, title) for news_id, title in targets])

        # 刷新主 session 中的 cover_url，后续逻辑若读取到旧值也能保持一致
        await db.commit()

    for news in inserted:
        count += 1
        await _emit("save", f"✓ {_short(news.title, 42)}")

    if skipped:
        await _emit("info", f"过滤重复新闻 {skipped} 条")

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
        logger.info("分类「%s」开始搜索：query=%r，max_results=15，hours=%s", cat.name, query, interval_hours)
        await _emit("thinking", f"分析「{cat.name}」领域最近 {interval_hours} 小时的热点…")
        await _emit("tool", f'search("{query}") → 调用搜索服务')
        try:
            search_results = await search_news(db, query, max_results=15, hours=interval_hours)
        except Exception as e:
            logger.error("分类「%s」搜索失败：%s", cat.name, e)
            await _emit("error", f"「{cat.name}」搜索失败: {e}")
            results[cat.name] = f"错误: {str(e)}"
            continue

        logger.info("分类「%s」搜索返回 %s 条候选", cat.name, len(search_results))
        await _emit("result", f"「{cat.name}」获取 {len(search_results)} 条候选结果")
        count = await process_search_results(db, cat.name, search_results, emit)
        await db.commit()
        results[cat.name] = count
        total_count += count
        logger.info("分类「%s」入库完成，新增 %s 条", cat.name, count)
        await _emit("result", f"「{cat.name}」入库完成，新增 {count} 条")

    env_vars["LAST_SEARCH_TIME"] = datetime.now().isoformat()
    write_env_file(env_vars)

    logger.info("采集任务结束：共新增 %s 条新闻，时间窗口 %s 小时", total_count, interval_hours)
    await _emit("success", f"采集任务完成，共新增 {total_count} 条新闻", total_new=total_count)

    return {
        "total_new": total_count,
        "details": results,
        "interval_hours": interval_hours,
        "search_time": env_vars["LAST_SEARCH_TIME"],
    }
