"""新闻向量索引器。

- 嵌入文本：title + summary + content[:600]
- 一条新闻一个文档级向量；内容变化（text_hash 改变）才重算
- 后台任务按批处理，失败只记录日志不空转
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import News, NewsEmbedding
from app.services import llm_client
from app.services.model_configs import get_active_endpoint
from app.services.vector_store import VectorRow, encode_vector, get_vector_store

logger = get_logger("embeddings")

BATCH_SIZE = 10
EMBED_CHAR_LIMIT = 600
MAX_ATTEMPTS = 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(read_env_file().get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (read_env_file().get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def compose_text(title: str | None, summary: str | None, content: str | None) -> str:
    """嵌入文本的唯一拼装入口：索引与待处理计数必须用同一份，否则哈希永远对不上。"""
    parts = [title or ""]
    if summary:
        parts.append(summary)
    if content:
        parts.append(content[:EMBED_CHAR_LIMIT])
    return "\n".join(part for part in parts if part).strip()


def embed_text(news: News) -> str:
    return compose_text(news.title, news.summary, news.content)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def indexer_enabled() -> bool:
    return _env_bool("AGENT_EMBED_ENABLED", True)


async def _embedding_ready(db: AsyncSession) -> dict | None:
    endpoint = await get_active_endpoint(db, "embedding")
    if not endpoint or not endpoint.get("api_key") or not endpoint.get("model_id"):
        return None
    return endpoint


async def pending_count(db: AsyncSession) -> int:
    rows = await db.execute(
        select(News.id, News.title, News.summary, News.content, NewsEmbedding.text_hash)
        .outerjoin(NewsEmbedding, NewsEmbedding.news_id == News.id)
        .where(News.is_deleted == 0)
    )
    total = 0
    for _, title, summary, content, existing_hash in rows.all():
        if existing_hash != text_hash(compose_text(title, summary, content)):
            total += 1
    return total


async def backfill(limit: int | None = None) -> dict:
    """为缺失或内容变化的新闻补向量。返回本次处理统计。"""
    try:
        from app.services.assist_service import is_assist

        if is_assist():
            return {"embedded": 0, "reason": "assist_mode"}
    except Exception:
        pass
    if not indexer_enabled():
        return {"embedded": 0, "reason": "disabled"}

    limit = limit or _env_int("AGENT_EMBED_MAX_PER_RUN", 200)
    started = time.time()

    async with get_session() as db:
        endpoint = await _embedding_ready(db)
        if not endpoint:
            return {"embedded": 0, "reason": "embedding_not_configured"}
        model = endpoint["model_id"]

        rows = await db.execute(
            select(News, NewsEmbedding.text_hash)
            .outerjoin(NewsEmbedding, NewsEmbedding.news_id == News.id)
            .where(News.is_deleted == 0)
            .order_by(News.collected_at.desc())
        )

        stale: list[tuple[News, str]] = []
        for news, existing_hash in rows.all():
            if len(stale) >= limit:
                break
            text = embed_text(news)
            if not text:
                continue
            digest = text_hash(text)
            if existing_hash == digest:
                continue
            stale.append((news, text))

    if not stale:
        return {"embedded": 0, "model": model, "elapsed_ms": int((time.time() - started) * 1000)}

    store = get_vector_store(await _engine())
    embedded = 0
    failed = 0
    for start in range(0, len(stale), BATCH_SIZE):
        batch = stale[start : start + BATCH_SIZE]
        texts = [text for _, text in batch]
        try:
            async with get_session() as db:
                vectors = await llm_client.embed(db, texts)
        except Exception as exc:
            failed += len(batch)
            logger.warning("向量化失败（第 %s 批）：%s", start // BATCH_SIZE + 1, exc)
            break

        if len(vectors) != len(batch):
            failed += len(batch)
            logger.warning("向量数量与请求不一致：%s != %s", len(vectors), len(batch))
            continue

        payload = []
        for (news, text), vector in zip(batch, vectors):
            if not vector:
                failed += 1
                continue
            payload.append(
                VectorRow(
                    news_id=news.id,
                    model=model,
                    dim=len(vector),
                    vector=encode_vector(vector),
                    text_hash=text_hash(text),
                )
            )
        if payload:
            try:
                await store.upsert(payload)
                embedded += len(payload)
            except Exception as exc:
                failed += len(payload)
                logger.warning("写入向量失败：%s", exc)

    logger.info(
        "向量索引：新增/更新 %s 条，失败 %s 条，模型 %s，耗时 %sms",
        embedded,
        failed,
        model,
        int((time.time() - started) * 1000),
    )
    return {
        "embedded": embedded,
        "failed": failed,
        "model": model,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


async def _engine():
    import app.database as database

    return database.engine


async def stats() -> dict:
    async with get_session() as db:
        endpoint = await _embedding_ready(db)
        store = get_vector_store(await _engine())
        data = await store.stats()
        data["enabled"] = indexer_enabled()
        data["configured"] = bool(endpoint)
        data["model"] = endpoint.get("model_id") if endpoint else None
        data["pending"] = await pending_count(db) if endpoint else 0
        return data


class EmbeddingIndexer:
    """后台索引器：启动后延迟执行一次，之后周期性补漏。"""

    def __init__(self, interval_seconds: int = 60):
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def run_once(self) -> dict:
        if self._lock.locked():
            return {"skipped": "already_running"}
        async with self._lock:
            return await backfill()

    async def _loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("后台向量索引异常：%s", exc)
            await asyncio.sleep(self._interval)

    def start(self):
        if self.running:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("向量索引器已启动（每 %s 秒检查一次）", self._interval)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("向量索引器已停止")


indexer = EmbeddingIndexer()
