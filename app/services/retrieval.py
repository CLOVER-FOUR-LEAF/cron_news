"""本地新闻库检索：标签过滤 + 词法/语义混合 + 时间线。

- 词法：SQLite FTS5 trigram；否则 LIKE
- 语义：向量存储（BLOB/numpy 或 pgvector），按当前启用的 embedding 模型
- 融合：Reciprocal Rank Fusion，k=60
- 时间线：按时间桶分别召回，保证「过去半个月」不会只返回某一天的 12 条
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import News, NewsTag
from app.services import fts, tag_service
from app.services.dedup import normalize_url, title_similarity

logger = get_logger("retrieval")

RRF_K = 60
LEXICAL_CANDIDATES = 40
SEMANTIC_CANDIDATES = 40
QUERY_VECTOR_TTL = 300
QUERY_VECTOR_CACHE_SIZE = 128

_query_cache: "OrderedDict[tuple[str, str], tuple[float, list[float]]]" = OrderedDict()


def active_engine():
    """运行时读取当前引擎：切库后 database.engine 会被替换，模块级导入会拿到旧对象。"""
    import app.database as database

    return database.engine


def current_backend() -> str:
    from app.services.vector_store import get_vector_store

    return get_vector_store(active_engine()).backend


def to_item(news: News, match: str, score: float | None = None) -> dict[str, Any]:
    item = {
        "ref": f"news:{news.id}",
        "id": news.id,
        "title": news.title,
        "summary": (news.summary or "")[:300],
        "short_summary": news.short_summary,
        "tags": news.tag_list,
        "source": news.source,
        "source_url": news.source_url,
        "cover_url": news.cover_url,
        "collected_at": news.collected_at.isoformat() if news.collected_at else None,
        "batch_id": news.batch_id,
        "is_read": news.is_read,
        "credibility": news.credibility,
        "match": match,
    }
    if score is not None:
        item["score"] = round(score, 4)
    return item


def refs_from_items(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    refs = {}
    for item in items:
        ref = item.get("ref")
        if not ref:
            continue
        refs[ref] = {
            "ref": ref,
            "kind": "news",
            "news_id": item.get("id"),
            "title": item.get("title"),
            "source": item.get("source"),
            "source_url": item.get("source_url"),
            "collected_at": item.get("collected_at"),
        }
    return refs


async def _load_ordered(db: AsyncSession, ids: list[int]) -> list[News]:
    if not ids:
        return []
    result = await db.execute(select(News).where(News.id.in_(ids), News.is_deleted == 0))
    by_id = {news.id: news for news in result.scalars().all()}
    return [by_id[i] for i in ids if i in by_id]


async def _fts_search(
    db: AsyncSession,
    terms: list[str],
    tag: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[News]:
    match = fts.build_match_query(terms)
    if not match:
        return []

    sql = (
        "SELECT n.id FROM news_fts "
        "JOIN news n ON n.id = news_fts.rowid "
        "WHERE news_fts MATCH :match AND n.is_deleted = 0"
    )
    params: dict[str, Any] = {"match": match, "limit": limit}
    if tag:
        sql += " AND EXISTS (SELECT 1 FROM news_tags nt WHERE nt.news_id = n.id AND nt.tag = :tag)"
        params["tag"] = tag
    if since is not None:
        sql += " AND n.collected_at >= :since"
        params["since"] = since
    if until is not None:
        sql += " AND n.collected_at < :until"
        params["until"] = until
    sql += " ORDER BY bm25(news_fts, 8.0, 2.0) LIMIT :limit"

    try:
        result = await db.execute(text(sql), params)
        ids = [int(row[0]) for row in result.all()]
    except Exception as exc:
        logger.debug("FTS 查询失败，回退 LIKE：%s", exc)
        return []
    return await _load_ordered(db, ids)


async def _like_search(
    db: AsyncSession,
    terms: list[str],
    tag: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[News]:
    conditions = [
        or_(News.title.contains(term), News.summary.contains(term), News.content.contains(term))
        for term in terms
    ]
    query = select(News).where(News.is_deleted == 0, and_(*conditions))
    if tag:
        query = query.where(
            News.id.in_(select(NewsTag.news_id).where(NewsTag.tag == tag))
        )
    if since is not None:
        query = query.where(News.collected_at >= since)
    if until is not None:
        query = query.where(News.collected_at < until)

    relevance = sum(case((News.title.contains(term), 1), else_=0) for term in terms)
    query = query.order_by(relevance.desc(), News.collected_at.desc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def lexical_search(
    db: AsyncSession,
    terms: list[str],
    *,
    tag: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = LEXICAL_CANDIDATES,
) -> tuple[list[News], str]:
    if not terms:
        return [], "empty"
    tag = tag_service.normalize_tag(tag or "")
    if fts.can_use_fts(terms):
        items = await _fts_search(db, terms, tag or None, since, until, limit)
        if items:
            return items, "fts"
    items = await _like_search(db, terms, tag or None, since, until, limit)
    return items, "like"


def _cache_get(model: str, query: str) -> list[float] | None:
    entry = _query_cache.get((model, query))
    if not entry:
        return None
    timestamp, vector = entry
    if time.time() - timestamp > QUERY_VECTOR_TTL:
        _query_cache.pop((model, query), None)
        return None
    _query_cache.move_to_end((model, query))
    return vector


def _cache_put(model: str, query: str, vector: list[float]) -> None:
    _query_cache[(model, query)] = (time.time(), vector)
    _query_cache.move_to_end((model, query))
    while len(_query_cache) > QUERY_VECTOR_CACHE_SIZE:
        _query_cache.popitem(last=False)


def semantic_enabled() -> bool:
    raw = (read_env_file().get("AGENT_EMBED_ENABLED", "") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def _query_vector(db: AsyncSession, query: str) -> tuple[list[float] | None, str | None]:
    from app.services import llm_client
    from app.services.model_configs import get_active_endpoint

    endpoint = await get_active_endpoint(db, "embedding")
    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        return None, None
    model = endpoint["model_id"]
    cached = _cache_get(model, query)
    if cached is not None:
        return cached, model
    try:
        vectors = await llm_client.embed(db, [query])
    except Exception as exc:
        logger.warning("查询向量化失败，仅使用词法检索：%s", exc)
        return None, model
    if not vectors or not vectors[0]:
        return None, model
    _cache_put(model, query, vectors[0])
    return vectors[0], model


def rrf_fuse(
    lexical_ids: list[int],
    semantic_ids: list[int],
    *,
    k: int = RRF_K,
) -> list[tuple[int, float, str]]:
    scores: dict[int, float] = {}
    origin: dict[int, str] = {}

    for rank, news_id in enumerate(lexical_ids, start=1):
        scores[news_id] = scores.get(news_id, 0.0) + 1.0 / (k + rank)
        origin[news_id] = "lexical"

    for rank, news_id in enumerate(semantic_ids, start=1):
        scores[news_id] = scores.get(news_id, 0.0) + 1.0 / (k + rank)
        origin[news_id] = "both" if news_id in origin else "semantic"

    ordered = sorted(scores.items(), key=lambda item: -item[1])
    return [(news_id, score, origin.get(news_id, "lexical")) for news_id, score in ordered]


async def _rank_window(
    db: AsyncSession,
    *,
    query: str,
    terms: list[str],
    tag: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    mode: str,
    query_vector: list[float] | None = None,
    model: str | None = None,
) -> tuple[list[tuple[News, float, str]], dict[str, Any]]:
    snapshot = {
        "lexical": {"mode": "none", "candidates": 0},
        "semantic": {"mode": "disabled", "candidates": 0},
    }
    lexical_items: list[News] = []
    if mode in {"hybrid", "lexical"}:
        lexical_items, lexical_mode = await lexical_search(
            db, terms, tag=tag, since=since, until=until, limit=LEXICAL_CANDIDATES
        )
        snapshot["lexical"] = {"mode": lexical_mode, "candidates": len(lexical_items)}

    semantic_items: list[News] = []
    semantic_scores: dict[int, float] = {}
    if mode in {"hybrid", "semantic"} and semantic_enabled():
        if query_vector is None:
            query_vector, model = await _query_vector(db, query)
        if query_vector and model:
            try:
                from app.services.vector_store import get_vector_store

                store = get_vector_store(active_engine())
                hits = await store.search(
                    query_vector,
                    model=model,
                    limit=SEMANTIC_CANDIDATES,
                    tag=tag,
                    since=since,
                    until=until,
                )
                semantic_scores = {hit.news_id: hit.score for hit in hits}
                semantic_items = await _load_ordered(db, [hit.news_id for hit in hits])
                snapshot["semantic"] = {"mode": store.backend, "candidates": len(semantic_items)}
            except Exception as exc:
                logger.warning("语义检索失败：%s", exc)
                snapshot["semantic"] = {"mode": "error", "candidates": 0}
        else:
            snapshot["semantic"] = {"mode": "unavailable", "candidates": 0}

    if mode == "lexical" or not semantic_items:
        ranked = [
            (news, semantic_scores.get(news.id, 0.0), snapshot["lexical"]["mode"])
            for news in lexical_items
        ]
    elif not lexical_items:
        ranked = [
            (news, semantic_scores.get(news.id, 0.0), "semantic") for news in semantic_items
        ]
    else:
        by_id = {news.id: news for news in lexical_items + semantic_items}
        ranked = [
            (by_id[news_id], score, match)
            for news_id, score, match in rrf_fuse(
                [news.id for news in lexical_items], [news.id for news in semantic_items]
            )
            if news_id in by_id
        ]

    return ranked[:limit], snapshot


async def search_news(
    db: AsyncSession,
    query: str,
    *,
    tag: str | None = None,
    days: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 8,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """mode: hybrid | lexical | semantic（默认 hybrid，语义不可用时自动降级）。"""
    terms = fts.split_terms(query)
    limit = max(1, min(int(limit or 8), 60))
    if not terms:
        return {"query": query, "items": [], "mode": "empty"}

    tag = tag_service.normalize_tag(tag or "")
    if days and since is None:
        since = datetime.now() - timedelta(days=max(1, min(int(days), 365)))

    query_vector, model = await _query_vector(db, query) if mode in {"hybrid", "semantic"} and semantic_enabled() else (None, None)
    ranked, snapshot = await _rank_window(
        db,
        query=query,
        terms=terms,
        tag=tag or None,
        since=since,
        until=until,
        limit=limit,
        mode=mode,
        query_vector=query_vector,
        model=model,
    )

    results = []
    seen_urls: set[str] = set()
    for news, score, match in ranked:
        if news.source_url:
            key = normalize_url(news.source_url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
        results.append(to_item(news, match, score if mode == "hybrid" else None))

    result_mode = mode
    if mode == "hybrid" and snapshot["semantic"]["mode"] in {"disabled", "unavailable", "error"}:
        result_mode = "lexical"

    return {
        "query": query,
        "mode": result_mode,
        "terms": terms,
        "tag": tag or None,
        "sources": snapshot,
        "items": results,
    }


async def list_recent(
    db: AsyncSession,
    *,
    tag: str | None = None,
    days: int = 1,
    unread_only: bool = False,
    limit: int = 10,
    until: datetime | None = None,
) -> dict[str, Any]:
    tag = tag_service.normalize_tag(tag or "")
    limit = max(1, min(int(limit or 10), 50))
    since = datetime.now() - timedelta(days=max(1, min(int(days or 1), 90)))
    query = select(News).where(News.is_deleted == 0, News.collected_at >= since)
    if tag:
        query = query.where(News.id.in_(select(NewsTag.news_id).where(NewsTag.tag == tag)))
    if until is not None:
        query = query.where(News.collected_at < until)
    if unread_only:
        query = query.where(News.is_read == 0)
    query = query.order_by(News.collected_at.desc()).limit(limit)

    result = await db.execute(query)
    items = [to_item(news, "recent") for news in result.scalars().all()]
    return {"items": items, "mode": "recent", "days": days, "tag": tag or None}


def _clean_timeline_terms(query: str) -> list[str]:
    """去掉「发展历程/进展/动态」等泛词，保留实体与主题，避免 LIKE 被泛词带偏。"""
    text = query or ""
    for word in (
        "发展历程", "发展史", "演变", "脉络", "时间线", "梳理", "总结", "回顾",
        "进展", "动态", "情况", "帮我", "一下", "过去", "最近", "近", "的", "新闻",
    ):
        text = text.replace(word, " ")
    return fts.split_terms(text) or fts.split_terms(query)


async def search_timeline(
    db: AsyncSession,
    query: str,
    *,
    since: datetime,
    until: datetime,
    tag: str | None = None,
    bucket_days: int | None = None,
    per_bucket: int = 8,
    max_events: int = 60,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """按时间桶召回，返回按日期排序的事件时间线与覆盖情况。"""
    now = datetime.now()
    if until > now:
        until = now
    days = max(1, (until - since).days)
    bucket_days = bucket_days or (1 if days <= 14 else (7 if days <= 60 else 14))
    bucket_days = max(1, min(int(bucket_days), 30))
    tag = tag_service.normalize_tag(tag or "")
    terms = _clean_timeline_terms(query)
    if not terms:
        terms = fts.split_terms(query)
    if not terms:
        return {
            "query": query,
            "items": [],
            "timeline": [],
            "buckets": [],
            "coverage": {"days_total": days, "days_with_news": 0},
        }

    query_vector, model = await _query_vector(db, query) if mode in {"hybrid", "semantic"} and semantic_enabled() else (None, None)
    bucket_count = max(1, (days + bucket_days - 1) // bucket_days)
    budget_per_bucket = max(2, min(per_bucket, max(2, max_events // bucket_count + 1)))

    buckets: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    days_with_news: set[str] = set()
    semantic_modes: set[str] = set()

    cursor = since
    while cursor < until:
        end = min(cursor + timedelta(days=bucket_days), until)
        ranked, snapshot = await _rank_window(
            db,
            query=query,
            terms=terms,
            tag=tag or None,
            since=cursor,
            until=end,
            limit=budget_per_bucket,
            mode=mode,
            query_vector=query_vector,
            model=model,
        )
        semantic_modes.add(snapshot["semantic"]["mode"])
        events = []
        for news, score, match in ranked:
            if news.source_url:
                key = normalize_url(news.source_url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
            if any(title_similarity(news.title, title) >= 0.86 for title in seen_titles):
                continue
            seen_titles.append(news.title)
            item = to_item(news, match, score if mode == "hybrid" else None)
            item["date"] = (news.collected_at or cursor).isoformat()
            item["bucket"] = cursor.isoformat()
            events.append(item)
            timeline.append(item)
            if news.collected_at:
                days_with_news.add(news.collected_at.date().isoformat())
        buckets.append(
            {
                "start": cursor.isoformat(),
                "end": end.isoformat(),
                "count": len(events),
                "events": events,
            }
        )
        cursor = end

    timeline.sort(key=lambda item: item.get("date") or "")
    if len(timeline) > max_events:
        step = len(timeline) / max_events
        timeline = [timeline[int(index * step)] for index in range(max_events)]

    empty_buckets = [bucket["start"] for bucket in buckets if bucket["count"] == 0]
    backend = "pgvector" if "pgvector" in semantic_modes else ("blob" if "blob" in semantic_modes else "lexical_only")
    return {
        "query": query,
        "tag": tag or None,
        "range": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": days,
            "bucket_days": bucket_days,
        },
        "buckets": buckets,
        "timeline": timeline,
        "items": timeline,
        "coverage": {
            "days_total": days,
            "days_with_news": len(days_with_news),
            "empty_buckets": empty_buckets,
            "backend": backend,
        },
        "sources": {
            "terms": terms,
            "semantic": backend,
        },
    }


async def status(db: AsyncSession) -> dict[str, Any]:
    from sqlalchemy import func

    from app.services.embeddings import pending_count
    from app.services.vector_store import get_vector_store

    total = int(await db.scalar(select(func.count()).where(News.is_deleted == 0)) or 0)
    store = get_vector_store(active_engine())
    try:
        vector_stats = await store.stats()
    except Exception as exc:
        vector_stats = {"backend": store.backend, "error": str(exc)}
    try:
        pending = await pending_count(db)
    except Exception:
        pending = 0

    return {
        "fts_available": fts.is_available(),
        "fts_tokenizer": "trigram" if fts.is_trigram() else ("unicode61" if fts.is_available() else "none"),
        "news_total": total,
        "vector": vector_stats,
        "pending_embeddings": pending,
        "semantic_enabled": semantic_enabled(),
    }
