"""BLOB + numpy 暴力余弦：SQLite / MySQL / MariaDB 通用实现。

5 万条 × 512 维约 100MB，一次全量扫描 5–20ms，个人规模足够。
矩阵按 (model, dim) 缓存，写入后失效。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import get_logger
from app.models import News, NewsEmbedding, NewsTag
from app.services.vector_store.base import VectorHit, VectorRow, VectorStore, decode_vector, encode_vector

logger = get_logger("vector.blob")

CACHE_TTL_SECONDS = 30


class BlobVectorStore(VectorStore):
    backend = "blob"
    ann = False

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._cache: dict[tuple[str, int], tuple[float, np.ndarray, np.ndarray]] = {}

    def _invalidate(self, model: str | None = None):
        if model is None:
            self._cache.clear()
            return
        for key in [key for key in self._cache if key[0] == model]:
            self._cache.pop(key, None)

    async def upsert(self, rows: list[VectorRow]) -> int:
        if not rows:
            return 0
        async with self._session_factory() as db:
            existing = await db.execute(
                select(NewsEmbedding.news_id).where(
                    NewsEmbedding.news_id.in_([row.news_id for row in rows])
                )
            )
            existing_ids = {int(item) for item in existing.scalars().all()}
            if existing_ids:
                await db.execute(
                    delete(NewsEmbedding).where(NewsEmbedding.news_id.in_(existing_ids))
                )
            db.add_all(
                [
                    NewsEmbedding(
                        news_id=row.news_id,
                        model=row.model,
                        dim=row.dim,
                        vector=row.vector,
                        text_hash=row.text_hash,
                    )
                    for row in rows
                ]
            )
            await db.commit()
        self._invalidate()
        return len(rows)

    async def delete(self, news_ids: list[int]) -> int:
        if not news_ids:
            return 0
        async with self._session_factory() as db:
            result = await db.execute(
                delete(NewsEmbedding).where(NewsEmbedding.news_id.in_(news_ids))
            )
            await db.commit()
        self._invalidate()
        return result.rowcount or 0

    async def _load_matrix(self, db: AsyncSession, model: str, dim: int):
        cached = self._cache.get((model, dim))
        now = time.time()
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], cached[2]

        result = await db.execute(
            select(NewsEmbedding.news_id, NewsEmbedding.vector).where(
                NewsEmbedding.model == model,
                NewsEmbedding.dim == dim,
            )
        )
        ids: list[int] = []
        vectors: list[np.ndarray] = []
        for news_id, blob in result.all():
            if not blob:
                continue
            array = decode_vector(blob)
            if array.size != dim:
                continue
            ids.append(int(news_id))
            vectors.append(array)

        id_array = np.asarray(ids, dtype=np.int64)
        matrix = (
            np.vstack(vectors).astype(np.float32)
            if vectors
            else np.zeros((0, dim), dtype=np.float32)
        )
        self._cache[(model, dim)] = (now, id_array, matrix)
        return id_array, matrix

    async def search(
        self,
        query_vector,
        *,
        model: str,
        limit: int,
        tag: str | None = None,
        since=None,
        until=None,
        exclude_ids: set[int] | None = None,
    ) -> list[VectorHit]:
        dim = len(query_vector)
        if dim == 0:
            return []
        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm > 0:
            query = query / norm

        async with self._session_factory() as db:
            id_array, matrix = await self._load_matrix(db, model, dim)
            if id_array.size == 0:
                return []

            # 先取全部允许的新闻 ID，再做精确掩码，避免 top-N 后过滤导致的召回塌陷
            stmt = select(News.id).where(News.is_deleted == 0)
            if tag:
                stmt = stmt.join(NewsTag, NewsTag.news_id == News.id).where(NewsTag.tag == tag)
            if since is not None:
                stmt = stmt.where(News.collected_at >= since)
            if until is not None:
                stmt = stmt.where(News.collected_at < until)
            allowed_ids = {int(item) for item in (await db.execute(stmt)).scalars().all()}
            if not allowed_ids:
                return []

            mask = np.isin(id_array, np.fromiter(allowed_ids, dtype=np.int64))
            sims = matrix @ query
            sims = np.where(mask, sims, -np.inf)
            if exclude_ids:
                exclude_mask = np.isin(id_array, np.fromiter(exclude_ids, dtype=np.int64))
                sims = np.where(exclude_mask, -np.inf, sims)
            count = min(max(limit, 1), int(mask.sum()))
            if count <= 0:
                return []
            top = np.argpartition(-sims, count - 1)[:count]
            top = top[np.argsort(-sims[top])]
            return [
                VectorHit(news_id=int(id_array[index]), score=float(sims[index]))
                for index in top
                if np.isfinite(sims[index])
            ]

    async def stats(self) -> dict:
        async with self._session_factory() as db:
            total = int(
                await db.scalar(select(func.count()).select_from(NewsEmbedding)) or 0
            )
            models = (
                await db.execute(
                    select(NewsEmbedding.model, NewsEmbedding.dim, func.count())
                    .group_by(NewsEmbedding.model, NewsEmbedding.dim)
                )
            ).all()
            news_total = int(await db.scalar(select(func.count()).select_from(News)) or 0)
        return {
            "backend": self.backend,
            "ann": self.ann,
            "vectors": total,
            "news_total": news_total,
            "models": [
                {"model": model, "dim": dim, "count": count}
                for model, dim, count in models
            ],
        }

    async def rebuild_index(self) -> dict:
        self._invalidate()
        return {"rebuilt": 0, "detail": "blob 后端无需重建索引"}
