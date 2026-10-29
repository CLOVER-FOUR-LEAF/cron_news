import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import News
from app.models.news_tag import NewsTag
from app.logging_setup import get_logger
from app.services import memory_service, tag_service, user_profile
from app.services.agent_prompts import get_agent_prompt
from app.services.llm_client import chat_text
from app.services.model_configs import get_active_endpoint

logger = get_logger("recommend")

EmitFn = Callable[..., Awaitable[None]]

CANDIDATE_POOL = 200
BATCH_SIZE = 20
RELATED_LIMIT = 6
PROFILE_CANDIDATE_LIMIT = 400


def _item(news: News, reason: str = "", score: float | None = None) -> dict[str, Any]:
    data = {
        "ref": f"news:{news.id}",
        "id": news.id,
        "title": news.title,
        "summary": (news.summary or "")[:300],
        "short_summary": news.short_summary,
        "tags": news.tag_list,
        "source": news.source,
        "source_url": news.source_url,
        "collected_at": news.collected_at.isoformat() if news.collected_at else None,
        "batch_id": news.batch_id,
        "read_at": news.read_at.isoformat() if news.read_at else None,
        "is_read": news.is_read,
        "is_reading": news.is_reading,
        "credibility": news.credibility,
        "related": [],
    }
    if reason:
        data["reason"] = reason
    if score is not None:
        data["score"] = round(score, 1)
    return data


def _tags(news: News) -> list[str]:
    return news.tag_list


async def _recent_read_context(db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_read == 1)
            .order_by(News.read_at.desc())
            .limit(40)
        )
    ).scalars().all()
    read_ids = {news.id for news in rows}
    related_counts: Counter = Counter()
    related_reason: dict[int, str] = {}
    tag_counts: Counter = Counter()
    for news in rows:
        for related_id in news.related_id_list:
            related_counts[related_id] += 1
            related_reason.setdefault(related_id, news.title)
        for tag in _tags(news):
            tag_counts[tag] += 1
    return {
        "ids": read_ids,
        "related_counts": related_counts,
        "related_reason": related_reason,
        "tags": tag_counts,
    }


def _recency_score(collected_at: datetime | None, now: datetime) -> float:
    if not collected_at:
        return 0.0
    hours = max(0.0, (now - collected_at).total_seconds() / 3600)
    return 14.0 * math.exp(-hours / 48.0)


def _reason_for(
    news: News,
    *,
    tag_affinity: float,
    top_tag: str | None,
    matched_topic: str | None,
    related_title: str | None,
    source_affinity: float,
    hours: float,
    memory_interest: str | None = None,
) -> str:
    reasons = []
    if memory_interest:
        reasons.append(f"记得你关注「{memory_interest}」")
    if top_tag and tag_affinity >= 0.35:
        reasons.append(f"你常看「{top_tag}」")
    if matched_topic and matched_topic != top_tag:
        reasons.append(f"与你读过的「{matched_topic}」相关")
    if related_title:
        reasons.append(f"与《{related_title[:24]}》相关")
    if source_affinity >= 0.45 and news.source:
        reasons.append(f"来自你常看的「{news.source}」")
    if hours <= 12:
        reasons.append("刚收录")
    if not reasons:
        reasons.append("为你补充的新内容")
    return " · ".join(reasons[:3])


def _score_candidate(
    news: News,
    *,
    tag_map: dict[str, float],
    source_map: dict[str, float],
    recent: dict[str, Any],
    negative: dict[str, Any],
    now: datetime,
) -> tuple[float, str | None, str | None, float, str | None]:
    tags = _tags(news)
    affinities = sorted((tag_map.get(tag, 0.0) for tag in tags), reverse=True)
    top_tag = None
    if tags:
        pairs = sorted(((tag_map.get(tag, 0.0), tag) for tag in tags), reverse=True)
        if pairs and pairs[0][0] > 0:
            top_tag = pairs[0][1]
    tag_avg = sum(affinities[:3]) / max(1, min(3, len(affinities))) if affinities else 0.0
    source_affinity = source_map.get(news.source or "", 0.0)
    hours = max(0.0, (now - news.collected_at).total_seconds() / 3600) if news.collected_at else 9999

    score = tag_avg * 34.0 + (affinities[0] if affinities else 0.0) * 8.0
    score += source_affinity * 10.0 + _recency_score(news.collected_at, now)

    matched_topic = None
    if recent["tags"] and tags:
        shared = set(tags) & set(recent["tags"].keys())
        if shared:
            matched_topic = max(shared, key=lambda tag: recent["tags"][tag])
            score += min(8.0, len(shared) * 2.0)

    related_count = recent["related_counts"].get(news.id, 0)
    related_title = None
    if related_count:
        score += min(20.0, 6.0 + related_count * 4.0)
        related_title = recent["related_reason"].get(news.id)
    if recent["ids"] & set(news.related_id_list):
        score += 8.0
    if news.credibility is not None:
        score += (float(news.credibility) - 50.0) * 0.08

    reasons = negative.get("reasons") or {}
    source_hits = negative["sources"].get(news.source or "", 0)
    if source_hits:
        score -= min(40.0, source_hits * 18.0)
    tag_hits = sum(negative["tags"].get(tag, 0) for tag in tags)
    if tag_hits:
        score -= min(36.0, tag_hits * 15.0)
    if reasons.get("source") and news.source in negative["sources"]:
        score -= 10.0
    if reasons.get("outdated") and hours > 48:
        score -= 12.0
    if reasons.get("shallow") and (news.credibility or 0) < 55:
        score -= 8.0

    positive = negative.get("positive") or {}
    if news.source in (positive.get("sources") or {}):
        score += 6.0
    if tags and set(tags) & set((positive.get("tags") or {}).keys()):
        score += 5.0

    memory_interest = None
    return score, matched_topic, related_title, hours, memory_interest


def _negative_context(profile: dict[str, Any]) -> dict[str, Any]:
    feedback = profile.get("feedback") or {}
    return {
        "ids": set(feedback.get("disliked_news_ids") or []),
        "sources": feedback.get("disliked_sources") or {},
        "tags": feedback.get("disliked_tags") or {},
        "reasons": feedback.get("negative_reasons") or {},
        "positive": {
            "sources": feedback.get("liked_sources") or {},
            "tags": feedback.get("liked_tags") or {},
        },
    }


async def _candidate_pool(
    db: AsyncSession,
    disliked_ids: set[int],
    *,
    batch_id: str | None,
    limit: int,
) -> tuple[str, list[News]]:
    now = datetime.now()
    if batch_id:
        rows = (
            await db.execute(
                select(News)
                .where(
                    News.is_deleted == 0,
                    News.is_read == 0,
                    News.batch_id == batch_id,
                )
                .order_by(News.collected_at.desc())
                .limit(PROFILE_CANDIDATE_LIMIT)
            )
        ).scalars().all()
        pool = [news for news in rows if news.id not in disliked_ids]
        if len(pool) >= max(5, limit // 2):
            return batch_id, pool

    pool: list[News] = []
    for days in (21, 60, 180):
        rows = (
            await db.execute(
                select(News)
                .where(
                    News.is_deleted == 0,
                    News.is_read == 0,
                    News.collected_at >= now - timedelta(days=days),
                )
                .order_by(News.collected_at.desc())
                .limit(PROFILE_CANDIDATE_LIMIT)
            )
        ).scalars().all()
        pool = [news for news in rows if news.id not in disliked_ids]
        if len(pool) >= 20:
            break
    if not pool:
        rows = (
            await db.execute(
                select(News)
                .where(News.is_deleted == 0, News.is_read == 0)
                .order_by(News.collected_at.desc())
                .limit(PROFILE_CANDIDATE_LIMIT)
            )
        ).scalars().all()
        pool = [news for news in rows if news.id not in disliked_ids]
    return "", pool


async def _related_for_items(db: AsyncSession, items: list[dict[str, Any]], limit: int = 3) -> None:
    if not items:
        return
    ids = {item["id"] for item in items}
    related_ids = set()
    for item in items:
        related_ids.update(item.get("related_ids") or [])
    news_map: dict[int, News] = {}
    if related_ids:
        rows = (
            await db.execute(select(News).where(News.id.in_(related_ids), News.is_deleted == 0))
        ).scalars().all()
        news_map = {news.id: news for news in rows}
    for item in items:
        related = []
        for rid in item.get("related_ids") or []:
            news = news_map.get(int(rid))
            if news and news.id not in ids:
                related.append(
                    {
                        "id": news.id,
                        "title": news.title,
                        "short_summary": news.short_summary,
                        "tags": news.tag_list,
                        "collected_at": news.collected_at.isoformat() if news.collected_at else None,
                    }
                )
            if len(related) >= limit:
                break
        if len(related) < limit and item.get("tags"):
            query = (
                select(News)
                .join(NewsTag, NewsTag.news_id == News.id)
                .where(
                    News.is_deleted == 0,
                    News.id != item["id"],
                    NewsTag.tag.in_(item["tags"][:3]),
                )
                .order_by(News.collected_at.desc())
                .limit(limit * 3)
            )
            for news in (await db.execute(query)).scalars().all():
                if news.id in ids or any(row["id"] == news.id for row in related):
                    continue
                related.append(
                    {
                        "id": news.id,
                        "title": news.title,
                        "short_summary": news.short_summary,
                        "tags": news.tag_list,
                        "collected_at": news.collected_at.isoformat() if news.collected_at else None,
                    }
                )
                if len(related) >= limit:
                    break
        item["related"] = related


async def personalized_recommendations(
    db: AsyncSession,
    limit: int = 10,
    *,
    mode: str = "",
) -> dict[str, Any]:
    """从最新采集批次中挑选最符合用户习惯的新闻，并附相关新闻。"""
    limit = max(1, min(int(limit or 10), 30))
    profile = await user_profile.compute_profile(db)
    negative = _negative_context(profile)
    batch_id = await tag_service.latest_batch_id(db)
    used_batch, candidates = await _candidate_pool(
        db, negative["ids"], batch_id=batch_id, limit=limit
    )

    if not candidates:
        return {
            "mode": mode,
            "batch_id": used_batch,
            "hint": "上一批新闻还没有未读内容，先让采集 Agent 更新一轮吧",
            "items": [],
        }

    recent = await _recent_read_context(db)
    tag_map = {item["tag"]: item["affinity"] for item in profile.get("tags") or []}
    source_map = {item["name"]: item["affinity"] for item in profile.get("sources") or []}

    memory_text = ""
    if memory_service.enabled():
        try:
            memory_text = await memory_service.read_memory()
        except Exception:
            memory_text = ""
    interest_terms = [
        term.lower() for term in memory_service.extract_terms(memory_text, ("watch", "interests"))
    ]
    avoid_terms = [
        term.lower() for term in memory_service.extract_terms(memory_text, ("avoid",))
    ]

    now = datetime.now()
    scored: list[tuple[float, News, str]] = []
    for news in candidates:
        score, matched_topic, related_title, hours, _ = _score_candidate(
            news,
            tag_map=tag_map,
            source_map=source_map,
            recent=recent,
            negative=negative,
            now=now,
        )
        text = " ".join([news.title or "", " ".join(_tags(news)), news.source or ""]).lower()
        memory_interest = next((term for term in interest_terms if term in text), None)
        if memory_interest:
            score += 6.0
        if any(term in text for term in avoid_terms):
            score -= 22.0
        top_tag = None
        if _tags(news):
            top_tag = max(_tags(news), key=lambda tag: tag_map.get(tag, 0.0))
            if tag_map.get(top_tag, 0.0) <= 0:
                top_tag = _tags(news)[0]
        reason = _reason_for(
            news,
            tag_affinity=tag_map.get(top_tag or "", 0.0),
            top_tag=top_tag,
            matched_topic=matched_topic,
            related_title=related_title,
            source_affinity=source_map.get(news.source or "", 0.0),
            hours=hours,
            memory_interest=memory_interest,
        )
        scored.append((score, news, reason))

    scored.sort(key=lambda item: -item[0])

    selected: list[tuple[float, News, str]] = []
    selected_ids: set[int] = set()
    tag_counts: Counter = Counter()
    source_counts: Counter = Counter()
    max_per_tag = max(2, math.ceil(limit / 3))
    max_per_source = max(3, math.ceil(limit / 2))

    for score, news, reason in scored:
        if news.id in selected_ids:
            continue
        primary = (_tags(news) or ["未标记"])[0]
        if tag_counts[primary] >= max_per_tag:
            continue
        if news.source and source_counts[news.source] >= max_per_source:
            continue
        selected.append((score, news, reason))
        selected_ids.add(news.id)
        tag_counts[primary] += 1
        if news.source:
            source_counts[news.source] += 1
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        for score, news, reason in scored:
            if len(selected) >= limit:
                break
            if news.id in selected_ids:
                continue
            selected.append((score, news, reason))
            selected_ids.add(news.id)

    items = [_item(news, reason, score) for score, news, reason in selected]
    for item, (_, news, _) in zip(items, selected):
        item["related_ids"] = news.related_id_list
    await _related_for_items(db, items)
    for item in items:
        item.pop("related_ids", None)

    if profile.get("has_signal"):
        totals = profile.get("totals") or {}
        source_label = f"上一批 {len(candidates)} 条未读" if used_batch else f"最近 {len(candidates)} 条未读"
        hint = (
            f"从{source_label}中，结合你读过的 {totals.get('read', 0)} 篇、"
            f"收藏 {totals.get('fav', 0)} 条与反馈挑选"
        )
        if profile.get("top_tags"):
            hint += f" · 偏好 {'、'.join(profile['top_tags'][:3])}"
    else:
        hint = "从上一批新闻中挑选，先读几篇、收藏或反馈几次，我会逐渐学会你的偏好"

    return {
        "mode": mode,
        "batch_id": used_batch,
        "hint": hint,
        "items": items,
        "profile": user_profile.profile_summary(profile),
    }


async def _ask_llm(db, system_prompt: str, prompt: str) -> str:
    return await chat_text(
        db,
        system_prompt,
        prompt,
        purpose="recommend",
        temperature=0.3,
        max_tokens=300,
        timeout=60.0,
    )


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
        logger.warning("智能推荐未执行：大语言模型未配置")
        return {"updated": 0}

    system_prompt = await get_agent_prompt(db, "recommend")
    memory_context = await memory_service.build_prompt_context(db, "recommend")
    if memory_context:
        system_prompt = (
            system_prompt
            + "\n\n"
            + memory_context
            + "\n\n判断新闻相关性时可以参考用户偏好，但不要因为用户喜欢某个标签就把无关内容硬凑进来。"
        )

    await _emit("thinking", "智能推荐 Agent 启动，分析新闻相关性…")
    logger.info("智能推荐开始")

    pool_result = await db.execute(
        select(News).where(News.is_deleted == 0).order_by(News.collected_at.desc()).limit(CANDIDATE_POOL)
    )
    pool = pool_result.scalars().all()

    if len(pool) < 2:
        await _emit("info", "新闻数量不足，跳过推荐计算")
        logger.info("智能推荐跳过：新闻数量不足（%s 条）", len(pool))
        return {"updated": 0}

    valid_ids = {n.id for n in pool}
    catalog_lines = []
    for n in pool:
        tags = "、".join(n.tag_list[:3])
        suffix = f" [{tags}]" if tags else ""
        catalog_lines.append(f"[{n.id}] {n.title}{suffix}")
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
        logger.info("智能推荐跳过：所有新闻均已计算推荐")
        return {"updated": 0}

    await _emit("info", f"候选新闻池 {len(pool)} 条，待计算 {len(targets)} 条")
    logger.info("智能推荐：候选池 %s 条，待计算 %s 条", len(pool), len(targets))

    updated = 0
    for news in targets:
        summary = (news.short_summary or news.title)[:160]
        tags_line = f"标签：{'、'.join(news.tag_list)}\n" if news.tag_list else ""
        prompt = f"""下面是新闻候选列表，每行格式为 [ID] 标题 [标签]：
{catalog}

请为以下新闻找出内容最相关的至多 {RELATED_LIMIT} 条新闻（可跨标签，依据主题、事件、人物的关联性判断）：
目标新闻 [{news.id}] {news.title}
{tags_line}摘要：{summary}

只输出相关新闻的 ID，用逗号分隔，不要输出任何其他文字。"""

        await _emit("tool", f'llm.relate([{news.id}]) "{news.title[:24]}…"')
        try:
            answer = await _ask_llm(db, system_prompt, prompt)
            related = _parse_ids(answer, valid_ids, news.id)
            news.related_ids = json.dumps(related)
            updated += 1
            await db.flush()
            await db.commit()
            logger.info("新闻[%s] 关联 %s 条 → %s", news.id, len(related), related)
            await _emit("save", f"✓ [{news.id}] 关联 {len(related)} 条 → {related}")
        except Exception as e:
            logger.error("新闻[%s] 推荐计算失败：%s", news.id, e)
            await _emit("error", f"[{news.id}] 推荐计算失败: {e}")

    await db.flush()
    logger.info("智能推荐结束：更新 %s 条新闻", updated)
    await _emit("success", f"智能推荐完成，更新 {updated} 条新闻的相关性", updated=updated)
    return {"updated": updated}
