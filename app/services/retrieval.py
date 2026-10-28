"""本地新闻库检索：词法 + 语义混合，RRF 融合。

- 词法：SQLite FTS5 trigram（≥3 字）；否则 LIKE
- 语义：向量存储（BLOB/numpy 或 pgvector），按当前启用的 embedding 模型
- 融合：Reciprocal Rank Fusion，k=60
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.category import get_category_by_name
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import News
from app.services import fts

logger = get_logger("retrieval")

RRF_K = 60
LEXICAL_CANDIDATES = 40
SEMANTIC_CANDIDATES = 40
QUERY_VECTOR_TTL = 300
QUERY_VECTOR_CACHE_SIZE = 128

_query_cache: "OrderedDict[tuple[str, str], tuple[float, list[float]]]" = OrderedDict()


def to_item(news: News, match: str, score: float | None = None) -> dict[str, Any]:
    item = {
        "ref": f"news:{news.id}",
        "id": news.id,
        "title": news.title,
        "summary": (news.summary or "")[:220],
        "category": news.category_name,
        "source": news.source,
        "source_url": news.source_url,
        "collected_at": news.collected_at.isoformat() if news.collected_at else None,
        "is_read": news.is_read,
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


async def _resolve_category(db: AsyncSession, category: str | None) -> tuple[bool, int | None]:
    if not category:
        return True, None
    row = await get_category_by_name(db, category.strip())
    if not row:
        return False, None
    return True, row.id


async def _load_ordered(db: AsyncSession, ids: list[int]) -> list[News]:
    if not ids:
        return []
    result = await db.execute(select(News).where(News.id.in_(ids), News.is_deleted == 0))
    by_id = {news.id: news for news in result.scalars().all()}
    return [by_id[i] for i in ids if i in by_id]


async def _fts_search(
    db: AsyncSession,
    terms: list[str],
    category_id: int | None,
    since: datetime | None,
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
    if category_id is not None:
        sql += " AND n.category_id = :category_id"
        params["category_id"] = category_id
    if since is not None:
        sql += " AND n.collected_at >= :since"
        params["since"] = since
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
    category_id: int | None,
    since: datetime | None,
    limit: int,
) -> list[News]:
    conditions = [
        or_(News.title.contains(term), News.summary.contains(term), News.content.contains(term))
        for term in terms
    ]
    query = select(News).where(News.is_deleted == 0, and_(*conditions))
    if category_id is not None:
        query = query.where(News.category_id == category_id)
    if since is not None:
        query = query.where(News.collected_at >= since)

    relevance = sum(case((News.title.contains(term), 1), else_=0) for term in terms)
    query = query.order_by(relevance.desc(), News.collected_at.desc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def lexical_search(
    db: AsyncSession,
    terms: list[str],
    *,
    category_id: int | None = None,
    since: datetime | None = None,
    limit: int = LEXICAL_CANDIDATES,
) -> tuple[list[News], str]:
    if not terms:
        return [], "empty"
    if fts.can_use_fts(terms):
        items = await _fts_search(db, terms, category_id, since, limit)
        if items:
            return items, "fts"
    items = await _like_search(db, terms, category_id, since, limit)
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
    """返回 [(news_id, score, match)]，按分数降序。"""
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


async def search_news(
    db: AsyncSession,
    query: str,
    *,
    category: str | None = None,
    days: int | None = None,
    limit: int = 8,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """mode: hybrid | lexical | semantic（默认 hybrid，语义不可用时自动降级）。"""
    terms = fts.split_terms(query)
    limit = max(1, min(int(limit or 8), 12))
    if not terms:
        return {"query": query, "items": [], "mode": "empty"}

    ok, category_id = await _resolve_category(db, category)
    if not ok:
        return {"query": query, "items": [], "mode": "empty", "note": f"分类「{category}」不存在"}

    since = None
    if days:
        since = datetime.now() - timedelta(days=max(1, min(int(days), 365)))

    snapshot = {
        "lexical": {"mode": "none", "candidates": 0},
        "semantic": {"mode": "disabled", "candidates": 0},
    }

    lexical_items: list[News] = []
    if mode in {"hybrid", "lexical"}:
        lexical_items, lexical_mode = await lexical_search(
            db, terms, category_id=category_id, since=since, limit=LEXICAL_CANDIDATES
        )
        snapshot["lexical"] = {"mode": lexical_mode, "candidates": len(lexical_items)}

    semantic_items: list[News] = []
    semantic_scores: dict[int, float] = {}
    if mode in {"hybrid", "semantic"} and semantic_enabled():
        vector, model = await _query_vector(db, query)
        if vector:
            try:
                from app.services.vector_store import get_vector_store

                import app.database as database

                store = get_vector_store(database.engine)
                hits = await store.search(
                    vector,
                    model=model,
                    limit=SEMANTIC_CANDIDATES,
                    category_id=category_id,
                    since=since,
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
            (news.id, semantic_scores.get(news.id, 0.0), snapshot["lexical"]["mode"])
            for news in lexical_items
        ]
        by_id = {news.id: news for news in lexical_items}
    elif len(lexical_items) == 0:
        ranked = [
            (news.id, semantic_scores.get(news.id, 0.0), "semantic") for news in semantic_items
        ]
        by_id = {news.id: news for news in semantic_items}
    else:
        ranked = rrf_fuse([news.id for news in lexical_items], [news.id for news in semantic_items])
        by_id = {news.id: news for news in lexical_items + semantic_items}

    results = []
    seen_urls: set[str] = set()
    for news_id, score, match in ranked:
        news = by_id.get(news_id)
        if not news:
            continue
        if news.source_url:
            if news.source_url in seen_urls:
                continue
            seen_urls.add(news.source_url)
        item = to_item(news, match, score if mode == "hybrid" else None)
        if mode == "hybrid" and score == 0.0:
            item["score"] = round(semantic_scores.get(news_id, 0.0), 4)
        results.append(item)
        if len(results) >= limit:
            break

    result_mode = mode
    if mode == "hybrid" and not semantic_items:
        result_mode = "lexical"

    return {
        "query": query,
        "mode": result_mode,
        "terms": terms,
        "sources": snapshot,
        "items": results,
    }


async def list_recent(
    db: AsyncSession,
    *,
    category: str | None = None,
    days: int = 1,
    unread_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    ok, category_id = await _resolve_category(db, category)
    if not ok:
        return {"items": [], "mode": "recent", "note": f"分类「{category}」不存在"}

    limit = max(1, min(int(limit or 10), 20))
    since = datetime.now() - timedelta(days=max(1, min(int(days or 1), 90)))
    query = select(News).where(News.is_deleted == 0, News.collected_at >= since)
    if category_id is not None:
        query = query.where(News.category_id == category_id)
    if unread_only:
        query = query.where(News.is_read == 0)
    query = query.order_by(News.collected_at.desc()).limit(limit)

    result = await db.execute(query)
    items = [to_item(news, "recent") for news in result.scalars().all()]
    return {"items": items, "mode": "recent", "days": days}


async def status(db: AsyncSession) -> dict[str, Any]:
    from sqlalchemy import func

    from app.services.embeddings import pending_count
    from app.services.vector_store import get_vector_store

    import app.database as database

    total = int(await db.scalar(select(func.count()).where(News.is_deleted == 0)) or 0)
    store = get_vector_store(database.engine)
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
