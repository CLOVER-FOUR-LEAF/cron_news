"""向量索引与检索维护脚本。

用法：
  uv run python scripts/reindex.py --fts            重建 FTS 索引
  uv run python scripts/reindex.py --embeddings     补齐缺失/过期的新闻向量
  uv run python scripts/reindex.py --all --limit 5000
  uv run python scripts/reindex.py --stats          查看后端与索引状态
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import engine, get_session, init_db  # noqa: E402
from app.services import embeddings, fts, retrieval  # noqa: E402
from app.services.vector_store import get_vector_store  # noqa: E402


async def rebuild_fts() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(fts.rebuild)
    print("FTS 索引已重建")


async def backfill(limit: int) -> None:
    result = await embeddings.backfill(limit=limit)
    print(f"向量补全：{result}")


async def show_stats() -> None:
    async with get_session() as db:
        data = await retrieval.status(db)
    print(data)


async def rebuild_vector() -> None:
    store = get_vector_store(engine)
    print(await store.rebuild_index())


async def main() -> None:
    parser = argparse.ArgumentParser(description="Cron News 检索索引维护")
    parser.add_argument("--fts", action="store_true", help="重建 FTS 索引")
    parser.add_argument("--embeddings", action="store_true", help="补齐新闻向量")
    parser.add_argument("--vector-index", action="store_true", help="重建向量派生索引（PG）")
    parser.add_argument("--stats", action="store_true", help="显示索引状态")
    parser.add_argument("--all", action="store_true", help="执行全部任务")
    parser.add_argument("--limit", type=int, default=1000, help="单次向量化上限")
    args = parser.parse_args()

    await init_db()

    if args.all or args.fts:
        await rebuild_fts()
    if args.all or args.embeddings:
        await backfill(args.limit)
    if args.all or args.vector_index:
        await rebuild_vector()
    if args.all or args.stats or not any([args.fts, args.embeddings, args.vector_index]):
        await show_stats()


if __name__ == "__main__":
    asyncio.run(main())
