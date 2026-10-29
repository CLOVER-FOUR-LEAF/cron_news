"""用户画像：从真实阅读行为、收藏、停留时长与反馈中计算标签偏好。

只做确定性统计，不调用模型；LLM 反思在 memory_service 中完成。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feedback, News, NewsTag
from app.services import tag_service


def _ratio(value: float, maximum: float) -> float:
    return round(value / maximum, 3) if maximum > 0 else 0.0


async def compute_profile(db: AsyncSession) -> dict[str, Any]:
    now = datetime.now()
    today_start = datetime.combine(date.today(), datetime.min.time())
    week_start = now - timedelta(days=7)

    total = int(await db.scalar(select(func.count()).where(News.is_deleted == 0)) or 0)
    read_count = int(
        await db.scalar(select(func.count()).where(News.is_deleted == 0, News.is_read == 1)) or 0
    )
    fav_count = int(
        await db.scalar(select(func.count()).where(News.is_deleted == 0, News.is_fav == 1)) or 0
    )
    later_count = int(
        await db.scalar(select(func.count()).where(News.is_deleted == 0, News.is_later == 1)) or 0
    )
    read_today = int(
        await db.scalar(
            select(func.count()).where(
                News.is_deleted == 0, News.is_read == 1, News.read_at >= today_start
            )
        )
        or 0
    )
    read_week = int(
        await db.scalar(
            select(func.count()).where(
                News.is_deleted == 0, News.is_read == 1, News.read_at >= week_start
            )
        )
        or 0
    )

    # 标签偏好：一篇新闻有多个标签，每个标签分别累计阅读/收藏/稍后
    tag_rows = (
        await db.execute(
            select(
                NewsTag.tag,
                func.sum(case((News.is_read == 1, 1), else_=0)),
                func.sum(case((News.is_fav == 1, 1), else_=0)),
                func.sum(case((News.is_later == 1, 1), else_=0)),
                func.count(News.id),
            )
            .join(News, News.id == NewsTag.news_id)
            .where(News.is_deleted == 0)
            .group_by(NewsTag.tag)
        )
    ).all()
    tags: list[dict[str, Any]] = []
    max_tag_score = 0.0
    for tag, reads, favs, laters, total_tag in tag_rows:
        reads, favs, laters = int(reads or 0), int(favs or 0), int(laters or 0)
        score = reads + favs * 2.4 + laters * 1.2
        max_tag_score = max(max_tag_score, score)
        tags.append(
            {
                "tag": tag,
                "read": reads,
                "fav": favs,
                "later": laters,
                "total": int(total_tag or 0),
                "affinity": score,
            }
        )
    for item in tags:
        item["affinity"] = _ratio(item["affinity"], max_tag_score)
        item["color"] = tag_service.tag_color(item["tag"])
    tags.sort(key=lambda item: (-item["affinity"], -item["read"]))

    # 来源偏好
    source_rows = (
        await db.execute(
            select(
                News.source,
                func.sum(case((News.is_read == 1, 1), else_=0)),
                func.sum(case((News.is_fav == 1, 1), else_=0)),
            )
            .where(News.is_deleted == 0, News.source.isnot(None))
            .group_by(News.source)
        )
    ).all()
    sources = []
    max_source_score = 0.0
    for name, reads, favs in source_rows:
        reads, favs = int(reads or 0), int(favs or 0)
        score = reads + favs * 2.4
        max_source_score = max(max_source_score, score)
        sources.append({"name": name, "read": reads, "fav": favs, "affinity": score})
    for item in sources:
        item["affinity"] = _ratio(item["affinity"], max_source_score)
    sources.sort(key=lambda item: (-item["affinity"], -item["read"]))

    # 最近阅读（停留时长、阅读时段、推荐关联）
    recent_rows = (
        await db.execute(
            select(News)
            .where(News.is_deleted == 0, News.is_read == 1)
            .order_by(News.read_at.desc())
            .limit(300)
        )
    ).scalars().all()
    recent_reads = []
    dwell_values: list[float] = []
    hour_counter: Counter = Counter()
    read_days: Counter = Counter()
    for news in recent_rows:
        if news.read_at:
            hour_counter[news.read_at.hour] += 1
            read_days[news.read_at.date().isoformat()] += 1
        if news.read_at and news.reading_at:
            seconds = (news.read_at - news.reading_at).total_seconds()
            if 5 <= seconds <= 6 * 3600:
                dwell_values.append(seconds)
        if len(recent_reads) < 20:
            recent_reads.append(
                {
                    "id": news.id,
                    "title": news.title,
                    "source": news.source,
                    "tags": news.tag_list,
                    "read_at": news.read_at.isoformat() if news.read_at else None,
                }
            )

    avg_dwell = int(sum(dwell_values) / len(dwell_values)) if dwell_values else None

    topic_counter: Counter = Counter()
    for item in tags:
        if item["read"] or item["fav"]:
            topic_counter[item["tag"]] += item["read"] + item["fav"] * 2
    topics = [{"tag": tag, "count": count} for tag, count in topic_counter.most_common(12)]

    # 反馈：正负信号落到新闻 / 来源 / 标签
    feedback_rows = (
        await db.execute(select(Feedback).order_by(Feedback.id.desc()).limit(120))
    ).scalars().all()
    disliked_news_ids: set[int] = set()
    liked_news_ids: set[int] = set()
    disliked_sources: Counter = Counter()
    liked_sources: Counter = Counter()
    disliked_tags: Counter = Counter()
    liked_tags: Counter = Counter()
    negative_reasons: Counter = Counter()
    positive_reasons: Counter = Counter()
    feedback_recent = []
    has_news_feedback = False

    news_ids = {
        item.target_id
        for item in feedback_rows
        if item.target_type == "news" and item.target_id
    }
    news_map: dict[int, News] = {}
    if news_ids:
        rows = await db.execute(select(News).where(News.id.in_(news_ids)))
        news_map = {news.id: news for news in rows.scalars().all()}

    for item in feedback_rows:
        is_negative = item.rating < 0
        if is_negative:
            negative_reasons[item.reason or "other"] += 1
        elif item.rating > 0:
            positive_reasons[item.reason or "helpful"] += 1

        if item.target_type == "news" and item.target_id:
            has_news_feedback = True
            news = news_map.get(item.target_id)
            if news:
                news_tags = news.tag_list
                if is_negative:
                    disliked_news_ids.add(news.id)
                    if news.source:
                        disliked_sources[news.source] += 1
                    for tag in news_tags:
                        disliked_tags[tag] += 1
                elif item.rating > 0:
                    liked_news_ids.add(news.id)
                    if news.source:
                        liked_sources[news.source] += 1
                    for tag in news_tags:
                        liked_tags[tag] += 1

        if len(feedback_recent) < 12:
            feedback_recent.append(
                {
                    "id": item.id,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "rating": item.rating,
                    "reason": item.reason,
                    "comment": item.comment,
                    "status": item.status,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
            )

    daily_reads = []
    for offset in range(13, -1, -1):
        day = (date.today() - timedelta(days=offset)).isoformat()
        daily_reads.append({"date": day, "count": read_days.get(day, 0)})

    top_tags = [item["tag"] for item in tags[:6] if item["affinity"] > 0]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": {
            "news": total,
            "read": read_count,
            "fav": fav_count,
            "later": later_count,
            "read_today": read_today,
            "read_week": read_week,
            "read_rate": round(read_count / total, 3) if total else 0.0,
            "avg_dwell_seconds": avg_dwell,
        },
        "tags": tags,
        "top_tags": top_tags,
        "sources": sources,
        "topics": topics,
        "top_topics": [item["tag"] for item in topics[:8]],
        "recent_reads": recent_reads,
        "daily_reads": daily_reads,
        "reading_hours": [{"hour": hour, "count": count} for hour, count in sorted(hour_counter.items())],
        "feedback": {
            "recent": feedback_recent,
            "negative_reasons": dict(negative_reasons),
            "positive_reasons": dict(positive_reasons),
            "disliked_news_ids": sorted(disliked_news_ids),
            "liked_news_ids": sorted(liked_news_ids),
            "disliked_sources": dict(disliked_sources),
            "liked_sources": dict(liked_sources),
            "disliked_tags": dict(disliked_tags),
            "liked_tags": dict(liked_tags),
        },
        "has_signal": bool(read_count >= 3 or fav_count or has_news_feedback),
    }


def render_auto_profile(profile: dict[str, Any]) -> str:
    totals = profile.get("totals") or {}
    lines = [
        f"- 自动生成于 {profile.get('generated_at')}（每 6 小时刷新）",
        f"- 共收录 {totals.get('news', 0)} 条，已读 {totals.get('read', 0)} 条"
        f"（阅读率 {int((totals.get('read_rate') or 0) * 100)}%），"
        f"近 7 天读 {totals.get('read_week', 0)} 条，收藏 {totals.get('fav', 0)} 条",
    ]
    if totals.get("avg_dwell_seconds"):
        lines.append(f"- 平均阅读停留约 {totals['avg_dwell_seconds']} 秒")

    tags = [item for item in profile.get("tags") or [] if item.get("read") or item.get("fav")]
    if tags:
        lines.append("- 标签偏好：" + "；".join(
            f"{item['tag']} 读{item['read']}/藏{item['fav']}（{int(item['affinity'] * 100)}%）"
            for item in tags[:6]
        ))

    topics = profile.get("topics") or []
    if topics:
        lines.append("- 关注话题：" + "、".join(f"{item['tag']}({item['count']})" for item in topics[:8]))

    sources = [item for item in profile.get("sources") or [] if item.get("read")]
    if sources:
        lines.append("- 常看来源：" + "、".join(
            f"{item['name']}({item['read']})" for item in sources[:4]
        ))

    recent = profile.get("recent_reads") or []
    if recent:
        lines.append("- 最近在读：" + "；".join(f"《{item['title'][:28]}》" for item in recent[:5]))

    feedback = profile.get("feedback") or {}
    negatives = feedback.get("negative_reasons") or {}
    if negatives:
        from app.services.memory_service import REASON_LABELS

        labels = [f"{REASON_LABELS.get(key, key)}×{count}" for key, count in negatives.items()]
        lines.append("- 负反馈信号：" + "、".join(labels[:6]))
    positives = feedback.get("positive_reasons") or {}
    if positives:
        lines.append(f"- 正反馈 {sum(positives.values())} 条")

    hours = profile.get("reading_hours") or []
    if hours:
        peak = max(hours, key=lambda item: item["count"])
        lines.append(f"- 阅读高峰：{peak['hour']}:00 前后")

    return "\n".join(lines)


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    totals = profile.get("totals") or {}
    return {
        "totals": totals,
        "top_tags": profile.get("top_tags") or [],
        "top_topics": profile.get("top_topics") or [],
        "top_sources": [
            item["name"] for item in (profile.get("sources") or [])[:4] if item.get("read")
        ],
        "recent_reads": [
            {"id": item["id"], "title": item["title"], "tags": item.get("tags")}
            for item in (profile.get("recent_reads") or [])[:8]
        ],
        "negative_reasons": (profile.get("feedback") or {}).get("negative_reasons") or {},
        "has_signal": profile.get("has_signal", False),
    }


async def get_profile_summary(db: AsyncSession) -> dict[str, Any]:
    return profile_summary(await compute_profile(db))
