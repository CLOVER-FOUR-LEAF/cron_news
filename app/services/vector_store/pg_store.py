"""PostgreSQL + pgvector：BLOB 为事实来源，pgvector 为可重建的派生索引。

设计取舍：
- `news_embeddings.vector`（BLOB）始终写入，pgvector 索引丢了可以重建；
  这样 SQLite -> PostgreSQL 的整库迁移逻辑不需要改变。
- 向量列使用 `vector`（不锁维度），换 embedding 模型不会被迫重建表；
  维度不一致的旧行在查询时按 dim 过滤。
- 扩展不可用、或表结构未准备好时自动回退到 BLOB 暴力扫描。
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import get_logger
from app.models import News, NewsEmbedding
from app.services.vector_store.base import VectorHit, VectorRow, vector_to_pg_literal
from app.services.vector_store.blob_store import BlobVectorStore

logger = get_logger("vector.pg")

INDEX_MIN_ROWS = 256


class PgVectorStore(BlobVectorStore):
    backend = "pgvector"
    ann = True

    def __init__(self, session_factory, ready: bool = True):
        super().__init__(session_factory)
        self._ready = ready
        self._index_ready = False

    async def _using_pgvector(self, db: AsyncSession) -> bool:
        if not self._ready:
            return False
        try:
            exists = await db.scalar(
                text("SELECT to_regclass('public.news_embedding_vec') IS NOT NULL")
            )
            return bool(exists)
        except Exception:
            self._ready = False
            logger.warning("pgvector 不可用，回退 BLOB 检索")
            return False

    async def ensure_schema(self, dim: int | None = None) -> dict:
        """创建/校正派生索引表。可重复调用。"""
        async with self._session_factory() as db:
            dialect = db.bind.dialect.name if db.bind is not None else ""
            if dialect != "postgresql":
                return {"ok": False, "detail": "非 PostgreSQL"}

            try:
                await db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await db.commit()
                self._ready = True
            except Exception as exc:
                self._ready = False
                logger.warning("创建 vector 扩展失败（可忽略，回退 BLOB）：%s", exc)
                return {"ok": False, "detail": str(exc)}

            await db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS news_embedding_vec ("
                    " news_id BIGINT PRIMARY KEY,"
                    " model VARCHAR(120) NOT NULL,"
                    " dim INTEGER NOT NULL,"
                    " embedding vector NOT NULL"
                    ")"
                )
            )
            await db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_news_embedding_vec_model "
                    "ON news_embedding_vec (model, dim)"
                )
            )
            await db.commit()
            self._index_ready = True

            await self.sync_from_blob()
            await self._maybe_create_ann_index()
            result = {"ok": True, "table": "news_embedding_vec"}
            if dim:
                result["dim"] = dim
            return result

    async def sync_from_blob(self) -> dict:
        """把 BLOB 中缺失/过期的行补进 pgvector 派生表。"""
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return {"synced": 0}
            rows = (
                await db.execute(
                    select(
                        NewsEmbedding.news_id,
                        NewsEmbedding.model,
                        NewsEmbedding.dim,
                        NewsEmbedding.vector,
                    )
                )
            ).all()
            synced = 0
            valid_ids = {int(news_id) for news_id, _, _, blob in rows if blob}
            for news_id, model, dim, blob in rows:
                if not blob:
                    continue
                literal = vector_to_pg_literal(np.frombuffer(blob, dtype="<f4"))
                await db.execute(
                    text(
                        "INSERT INTO news_embedding_vec (news_id, model, dim, embedding) "
                        "VALUES (:news_id, :model, :dim, CAST(:embedding AS vector)) "
                        "ON CONFLICT (news_id) DO UPDATE SET "
                        "model = EXCLUDED.model, dim = EXCLUDED.dim, embedding = EXCLUDED.embedding"
                    ),
                    {
                        "news_id": int(news_id),
                        "model": model,
                        "dim": int(dim),
                        "embedding": literal,
                    },
                )
                synced += 1

            # 清理 BLOB 中已不存在的派生行（新闻被删或向量已移除）
            stale = (
                await db.execute(
                    text("SELECT news_id FROM news_embedding_vec")
                )
            ).scalars().all()
            stale_ids = [int(item) for item in stale if int(item) not in valid_ids]
            if stale_ids:
                await db.execute(
                    text("DELETE FROM news_embedding_vec WHERE news_id = ANY(:ids)"),
                    {"ids": stale_ids},
                )
            await db.commit()
        self._invalidate()
        if synced:
            logger.info("pgvector 派生索引已同步 %s 条", synced)
        return {"synced": synced}

    async def _maybe_create_ann_index(self) -> None:
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return
            try:
                count = int(
                    await db.scalar(text("SELECT count(*) FROM news_embedding_vec")) or 0
                )
                if count < INDEX_MIN_ROWS:
                    return
                # vector 列不锁维度时无法直接建 HNSW；用表达式索引按维度分桶代价高。
                # 这里保留顺序扫描 + 分区过滤，规模足够；需要 ANN 时再显式调用 create_ann_index。
            except Exception as exc:
                logger.debug("检查 pgvector 索引失败：%s", exc)

    async def create_ann_index(self, dim: int) -> dict:
        """为固定维度创建 HNSW 索引（表达式索引）。"""
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return {"ok": False, "detail": "pgvector 不可用"}
            try:
                await db.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_news_embedding_vec_hnsw "
                        f"ON news_embedding_vec USING hnsw ((embedding::vector({int(dim)})) vector_cosine_ops) "
                        f"WHERE dim = {int(dim)}"
                    )
                )
                await db.commit()
                return {"ok": True, "index": "ix_news_embedding_vec_hnsw"}
            except Exception as exc:
                logger.warning("创建 HNSW 索引失败：%s", exc)
                return {"ok": False, "detail": str(exc)}

    async def upsert(self, rows: list[VectorRow]) -> int:
        count = await super().upsert(rows)
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return count
            for row in rows:
                literal = vector_to_pg_literal(np.frombuffer(row.vector, dtype="<f4"))
                await db.execute(
                    text(
                        "INSERT INTO news_embedding_vec (news_id, model, dim, embedding) "
                        "VALUES (:news_id, :model, :dim, CAST(:embedding AS vector)) "
                        "ON CONFLICT (news_id) DO UPDATE SET "
                        "model = EXCLUDED.model, dim = EXCLUDED.dim, embedding = EXCLUDED.embedding"
                    ),
                    {
                        "news_id": row.news_id,
                        "model": row.model,
                        "dim": row.dim,
                        "embedding": literal,
                    },
                )
            await db.commit()
        self._invalidate()
        return count

    async def delete(self, news_ids: list[int]) -> int:
        count = await super().delete(news_ids)
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return count
            await db.execute(
                text("DELETE FROM news_embedding_vec WHERE news_id = ANY(:ids)"),
                {"ids": news_ids},
            )
            await db.commit()
        return count

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
        async with self._session_factory() as db:
            if not await self._using_pgvector(db):
                return await super().search(
                    query_vector,
                    model=model,
                    limit=limit,
                    tag=tag,
                    since=since,
                    until=until,
                    exclude_ids=exclude_ids,
                )

            literal = vector_to_pg_literal(np.asarray(query_vector, dtype=np.float32))
            sql = (
                "SELECT v.news_id, (v.embedding <=> CAST(:q AS vector)) AS dist "
                "FROM news_embedding_vec v "
                "JOIN news n ON n.id = v.news_id "
                "WHERE v.model = :model AND v.dim = :dim AND n.is_deleted = 0"
            )
            params: dict[str, Any] = {
                "q": literal,
                "model": model,
                "dim": dim,
                "limit": max(limit, 1),
            }
            if tag:
                sql += (
                    " AND EXISTS (SELECT 1 FROM news_tags nt "
                    "WHERE nt.news_id = n.id AND nt.tag = :tag)"
                )
                params["tag"] = tag
            if since is not None:
                sql += " AND n.collected_at >= :since"
                params["since"] = since
            if until is not None:
                sql += " AND n.collected_at < :until"
                params["until"] = until
            sql += " ORDER BY dist LIMIT :limit"

            rows = (await db.execute(text(sql), params)).all()

        hits = [
            VectorHit(news_id=int(news_id), score=1.0 - float(dist))
            for news_id, dist in rows
            if not (exclude_ids and int(news_id) in exclude_ids)
        ]
        return hits[:limit]

    async def stats(self) -> dict:
        data = await super().stats()
        async with self._session_factory() as db:
            indexed = 0
            try:
                if await self._using_pgvector(db):
                    indexed = int(
                        await db.scalar(text("SELECT count(*) FROM news_embedding_vec")) or 0
                    )
            except Exception:
                indexed = 0
        data.update({"backend": self.backend, "ann": False, "indexed": indexed})
        return data

    async def rebuild_index(self) -> dict:
        await self.ensure_schema()
        result = await self.sync_from_blob()
        self._invalidate()
        return {"rebuilt": result.get("synced", 0)}
