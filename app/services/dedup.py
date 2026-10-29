"""采集去重：URL / 标题指纹 + 语义向量近邻。

- 标题与 URL 去重不依赖模型，始终可用；
- 配置了 embedding 模型时，再用余弦相似度识别「同一事件、不同标题」的重复报道；
- 只与最近 14 天的新闻比较，避免历史长尾拖慢采集。
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import News
from app.services import llm_client
from app.services.model_configs import get_active_endpoint
from app.services.vector_store import get_vector_store

logger = get_logger("dedup")

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "referrer", "share", "share_token",
    "timestamp", "_t", "fbclid", "gclid",
}
TITLE_SUFFIX = re.compile(r"\s*[-_|｜—–]\s*[^-_|｜—–]{2,24}$")


def _env_float(name: str, default: float) -> float:
    try:
        return float(read_env_file().get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (read_env_file().get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return url.strip().lower()
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        ("https", host, path, "", urlencode(sorted(query)), "")
    )


def normalize_title(title: str | None) -> str:
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = TITLE_SUFFIX.sub("", text)
    text = text.lower()
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text


def _shingles(text: str, n: int = 2) -> set[str]:
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[index : index + n] for index in range(len(text) - n + 1)}


def title_similarity(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter, longer = sorted((a, b), key=len)
        if len(shorter) >= 10 and len(shorter) / len(longer) >= 0.72:
            return 0.94
    sa, sb = _shingles(a), _shingles(b)
    jaccard = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jaccard, ratio * 0.95)


def _cosine(left: list[float], right: list[float]) -> float:
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


async def _semantic_duplicates(
    db: AsyncSession,
    candidates: list[dict[str, Any]],
    *,
    threshold: float,
) -> dict[int, dict[str, Any]]:
    """返回 candidate index -> {reason, duplicate_of, score}。失败时返回空字典。"""
    if not candidates:
        return {}
    endpoint = await get_active_endpoint(db, "embedding")
    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        return {}

    texts = [
        f"{item.get('title') or ''}\n{(item.get('snippet') or '')[:240]}".strip()
        for item in candidates
    ]
    try:
        vectors = await llm_client.embed(db, texts)
    except Exception as exc:
        logger.info("语义去重跳过（向量化失败）：%s", exc)
        return {}
    if len(vectors) != len(candidates):
        return {}

    model = endpoint["model_id"]
    store = get_vector_store(_engine())
    since = datetime.now() - timedelta(days=14)
    duplicates: dict[int, dict[str, Any]] = {}
    accepted_vectors: list[tuple[int, list[float], str]] = []

    for index, (candidate, vector) in enumerate(zip(candidates, vectors)):
        if not vector:
            continue
        try:
            hits = await store.search(vector, model=model, limit=2, since=since)
        except Exception as exc:
            logger.debug("语义去重检索失败：%s", exc)
            hits = []
        if hits and hits[0].score >= threshold:
            duplicates[index] = {
                "reason": f"语义重复（{hits[0].score:.3f}）",
                "duplicate_of": hits[0].news_id,
                "score": hits[0].score,
            }
            continue

        best = None
        for prev_index, prev_vector, prev_title in accepted_vectors:
            score = _cosine(vector, prev_vector)
            if score >= threshold and (best is None or score > best[1]):
                best = (prev_index, score, prev_title)
        if best:
            duplicates[index] = {
                "reason": f"与本批次《{best[2][:30]}》语义重复（{best[1]:.3f}）",
                "duplicate_of": None,
                "score": best[1],
            }
            continue
        accepted_vectors.append((index, vector, str(candidate.get("title") or "")))

    return duplicates


def _engine():
    import app.database as database

    return database.engine


async def dedupe_candidates(
    db: AsyncSession,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """对候选列表去重。

    返回 {accepted, duplicates, stats}。accepted 保留原始字段。
    """
    if not candidates:
        return {"accepted": [], "duplicates": [], "stats": {"accepted": 0, "duplicates": 0}}

    title_threshold = _env_float("AGENT_TITLE_DUP_THRESHOLD", 0.82)
    semantic_threshold = _env_float("AGENT_DEDUP_THRESHOLD", 0.92)
    semantic_enabled = _env_bool("AGENT_SEMANTIC_DEDUP", True)

    since = datetime.now() - timedelta(days=14)
    rows = (
        await db.execute(
            select(News.id, News.title, News.source_url)
            .where(News.is_deleted == 0, News.collected_at >= since)
            .order_by(News.collected_at.desc())
            .limit(1200)
        )
    ).all()
    existing_urls = {normalize_url(url) for _, _, url in rows if url}
    existing_titles = [(news_id, title or "") for news_id, title, _ in rows]

    duplicates: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    accepted_by_title: list[tuple[int, str]] = []

    for candidate in candidates:
        url_key = normalize_url(str(candidate.get("url") or ""))
        title = str(candidate.get("title") or "").strip()
        reason = ""
        duplicate_of = None

        if url_key and url_key in existing_urls:
            reason, duplicate_of = "URL 已收录", None
        else:
            for news_id, existing_title in existing_titles:
                if title_similarity(title, existing_title) >= title_threshold:
                    reason, duplicate_of = "标题与已有新闻高度相似", news_id
                    break
            if not reason:
                for index, existing_title in accepted_by_title:
                    if title_similarity(title, existing_title) >= title_threshold:
                        reason, duplicate_of = (
                            f"与本批次第 {index + 1} 条标题高度相似",
                            None,
                        )
                        break

        if reason:
            duplicates.append({"hit": candidate, "reason": reason, "duplicate_of": duplicate_of})
            continue
        accepted.append(candidate)
        accepted_by_title.append((len(accepted) - 1, title))
        if url_key:
            existing_urls.add(url_key)

    if semantic_enabled and accepted:
        semantic_dups = await _semantic_duplicates(db, accepted, threshold=semantic_threshold)
        if semantic_dups:
            kept: list[dict[str, Any]] = []
            for index, item in enumerate(accepted):
                dup = semantic_dups.get(index)
                if dup:
                    duplicates.append({"hit": item, "reason": dup["reason"], "duplicate_of": dup.get("duplicate_of")})
                else:
                    kept.append(item)
            accepted = kept

    stats = {
        "candidates": len(candidates),
        "accepted": len(accepted),
        "duplicates": len(duplicates),
        "reasons": dict(Counter(item["reason"].split("（")[0] for item in duplicates)),
    }
    return {"accepted": accepted, "duplicates": duplicates, "stats": stats}


def tags_from_news(news: News) -> list[str]:
    try:
        parsed = json.loads(news.tags or "[]")
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
