from datetime import datetime, date
from typing import Any, Callable, Awaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import News, Brief
from app.logging_setup import get_logger
from app.services import memory_service, tag_service
from app.services.agent_prompts import get_agent_prompt
from app.services.llm_client import chat
from app.services.model_configs import get_active_endpoint

logger = get_logger("brief")

EmitFn = Callable[..., Awaitable[None]]

# 每日简报固定存放在这一个「标签」下：全站一天只有一篇，不再按标签拆分
DAILY_BRIEF_KEY = "每日"
# 送给模型的素材上限（按可信度与时间排序后截取）
MAX_BRIEF_ITEMS = 40


async def _llm_endpoint(db) -> dict | None:
    return await get_active_endpoint(db, "llm")


async def _ask_llm(db, system_prompt: str, prompt: str) -> tuple[str, str]:
    """返回 (正文, finish_reason)。部分厂商会对素材触发内容审核，需要单独识别。"""
    result = await chat(
        db,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        purpose="brief",
        temperature=0.5,
        max_tokens=4096,
        timeout=180.0,
    )
    return (result.content or "").strip(), result.finish_reason


def _blocked_by_filter(content: str, finish_reason: str) -> bool:
    if finish_reason == "content_filter":
        return True
    lowered = (content or "").lower()
    return "request was rejected" in lowered or "considered high risk" in lowered


def _news_title_line(news: News) -> str:
    tags = "、".join(news.tag_list[:3]) or "无标签"
    return f"- [{tags}] {news.title}"


def _fallback_brief(items: list[News], today: date) -> str:
    """模型审核不通过时的兜底：用已入库的标题与摘要拼一份可读简报。"""
    lines = [
        f"# 每日简报 · {today.isoformat()}",
        "",
        "> 本期由平台自动汇总生成（模型内容审核未通过，未做额外评述）。",
        "",
        "## 今日要闻",
        "",
    ]
    for news in items[:8]:
        tags = "、".join(news.tag_list[:3]) or "未标注"
        summary = (news.short_summary or "").strip()
        lines.append(f"### {news.title}")
        lines.append(f"- 标签：{tags} · 可信度：{news.credibility or 0}")
        if summary:
            lines.append(f"- {summary}")
        lines.append("")
    return "\n".join(lines).strip()


def _news_line(news: News) -> str:
    tags = "、".join(news.tag_list[:3]) or "无标签"
    credibility = news.credibility or 0
    summary = (news.short_summary or "").strip()[:110]
    return f"- [{tags} · 可信 {credibility}] {news.title}：{summary}"


def _select_news(today_news: list[News]) -> list[News]:
    """按可信度与收录时间排序，挑出写简报用的素材。"""
    ordered = sorted(
        today_news,
        key=lambda news: (
            -(news.credibility or 0),
            -(news.collected_at.timestamp() if news.collected_at else 0),
        ),
    )
    return ordered[:MAX_BRIEF_ITEMS]


async def run_brief_task(db: AsyncSession, emit: EmitFn | None = None) -> dict[str, Any]:
    async def _emit(event_type: str, text: str, **extra: Any):
        if emit:
            await emit(event_type, text, **extra)

    if not await _llm_endpoint(db):
        await _emit("error", "大语言模型未配置，无法生成每日简报")
        logger.warning("每日简报未执行：大语言模型未配置")
        return {"generated": 0}

    system_prompt = await get_agent_prompt(db, "brief")
    today = date.today()
    system_prompt = system_prompt.replace("{日期}", today.isoformat()).replace("{date}", today.isoformat())
    memory_context = await memory_service.build_prompt_context(db, "brief")
    if memory_context:
        system_prompt = (
            system_prompt
            + "\n\n"
            + memory_context
            + "\n\n可以侧重用户关注的主题，但不要遗漏当日真正重要的事件，也不要歪曲事实。"
        )

    await _emit("thinking", "每日简报 Agent 启动，汇总今日全部新闻…")

    today_start = datetime.combine(today, datetime.min.time())
    logger.info("每日简报开始：日期=%s", today.isoformat())

    result = await db.execute(
        select(News)
        .where(News.is_deleted == 0, News.collected_at >= today_start)
        .order_by(News.collected_at.desc())
        .limit(200)
    )
    today_news = list(result.scalars().all())
    if not today_news:
        await _emit("info", "今日没有新闻，跳过简报")
        return {"generated": 0}

    selected = _select_news(today_news)
    tag_items = tag_service.count_tags(today_news, limit=12)
    tag_text = "、".join(f"{item['tag']}×{item['count']}" for item in tag_items) or "暂无标签"
    news_lines = "\n".join(_news_line(news) for news in selected)

    prompt = f"""今天是 {today.isoformat()}。
今日共收录 {len(today_news)} 条新闻，以下是按可信度与重要性挑选出的 {len(selected)} 条素材：
{news_lines}

今日标签分布：{tag_text}

请撰写今天唯一的一份「每日简报」，要求：
1. 不要按标签拆成多篇，所有内容整合进同一篇简报；
2. 严格按既定结构输出：标题「# 每日简报 · {today.isoformat()}」→ 导语 → 「## 今日要闻」→「## 串联与影响」→「## 值得继续关注」→「## 编辑说明」；
3. 在「今日要闻」中挑选 5-8 条真正重要的事件，按重要性排序，每条 2-3 句，讲清事实、关键数字与意义；
4. 不要机械罗列所有新闻；营销软文、重复报道与低可信内容已由平台过滤、不在素材中，
   可在「编辑说明」里笼统说明，不要编造被排除的具体条目；
5. 全文 700-1100 字，语言精炼、客观。"""

    await _emit("tool", f'llm.brief("今日 {len(today_news)} 条 / 精选 {len(selected)} 条")')
    try:
        content, finish_reason = await _ask_llm(db, system_prompt, prompt)
    except Exception as e:
        logger.error("每日简报生成失败：%s", e)
        await _emit("info", "模型调用失败，改用平台摘要兜底")
        content, finish_reason = "", "error"

    if _blocked_by_filter(content, finish_reason):
        # 部分厂商（如 MiMo）会对含敏感人物/事件摘要的素材整段拒答。
        # 去掉记忆、摘要与可能触发的表述，只用标题重试一次。
        logger.warning("每日简报触发内容审核（%s），改用标题素材重试", finish_reason or "rejected")
        await _emit("info", "模型内容审核未通过，正在用标题级素材重试…")
        safe_prompt = (
            f"今天是 {today.isoformat()}。今日共收录 {len(today_news)} 条新闻，标题如下：\n"
            + "\n".join(_news_title_line(news) for news in selected)
            + "\n\n请写一份客观、克制的每日简报：概括今天的主要事件，"
            "不要复述原文细节，不要涉及暴力、冲突或未经证实的信息。"
        )
        try:
            content, finish_reason = await _ask_llm(
                db,
                "你是一位中文新闻编辑。只根据给定标题撰写每日简报，语言客观、克制。",
                safe_prompt,
            )
        except Exception as e:
            logger.warning("每日简报重试失败：%s", e)
            content = ""

    if _blocked_by_filter(content, finish_reason) or len(content) < 120:
        logger.warning(
            "每日简报模型输出不可用（finish=%s，%s 字），使用确定性摘要兜底",
            finish_reason,
            len(content or ""),
        )
        await _emit("info", "模型未能给出可用简报，已改用平台摘要兜底")
        content = _fallback_brief(selected, today)

    if len(content) < 120:
        logger.error("每日简报内容过短，已放弃写入")
        await _emit("error", "每日简报内容过短，已放弃写入")
        return {"generated": 0}

    existing = await db.execute(
        select(Brief).where(
            Brief.category_name == DAILY_BRIEF_KEY, Brief.brief_date == today.isoformat()
        )
    )
    for old in existing.scalars().all():
        await db.delete(old)
    db.add(
        Brief(
            category_name=DAILY_BRIEF_KEY,
            brief_date=today.isoformat(),
            content=content,
            source="自主",
        )
    )
    await db.flush()
    await db.commit()
    logger.info("每日简报已生成：%s 字", len(content))
    await _emit("save", "✓ 今日简报已生成")
    await _emit("success", "每日简报完成", generated=1)
    return {"generated": 1}


async def get_daily_brief(db: AsyncSession, brief_date: str | None = None) -> Brief | None:
    """读取统一每日简报：不传日期取最新一期。"""
    if brief_date:
        return await get_brief_by_date(db, DAILY_BRIEF_KEY, brief_date)
    return await get_latest_brief(db, DAILY_BRIEF_KEY)


async def get_daily_brief_dates(db: AsyncSession) -> list[str]:
    return await get_brief_dates(db, DAILY_BRIEF_KEY)


async def get_brief_dates(db: AsyncSession, tag: str) -> list[str]:
    result = await db.execute(
        select(Brief.brief_date)
        .where(Brief.category_name == tag)
        .order_by(Brief.brief_date.desc())
    )
    return [row[0] for row in result.all()]


async def get_brief_by_date(db: AsyncSession, tag: str, brief_date: str) -> Brief | None:
    result = await db.execute(
        select(Brief)
        .where(Brief.category_name == tag, Brief.brief_date == brief_date)
        .order_by(Brief.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_latest_brief(db: AsyncSession, tag: str) -> Brief | None:
    result = await db.execute(
        select(Brief)
        .where(Brief.category_name == tag)
        .order_by(Brief.brief_date.desc(), Brief.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()
