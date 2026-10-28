"""向量存储工厂：按当前连接的数据库方言选择实现。

- sqlite / mysql / mariadb -> BlobVectorStore（numpy 暴力余弦）
- postgresql               -> PgVectorStore（pgvector 派生索引，不可用时自动回退）
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.services.vector_store.base import (
    VectorHit,
    VectorRow,
    VectorStore,
    decode_vector,
    encode_vector,
)
from app.services.vector_store.blob_store import BlobVectorStore
from app.services.vector_store.pg_store import PgVectorStore

logger = get_logger("vector")

_store: VectorStore | None = None
_store_key: str = ""


def dialect_name(engine) -> str:
    try:
        return engine.dialect.name
    except Exception:
        return "sqlite"


def get_vector_store(engine, *, refresh: bool = False) -> VectorStore:
    """按引擎方言返回单例。切库后 engine 变化会自动重建。"""
    global _store, _store_key

    from app.database import async_session

    key = f"{dialect_name(engine)}:{id(engine)}"
    if _store is not None and _store_key == key and not refresh:
        return _store

    session_factory = async_session
    if key.startswith("postgresql"):
        store: VectorStore = PgVectorStore(session_factory)
    else:
        store = BlobVectorStore(session_factory)

    _store = store
    _store_key = key
    logger.info("向量存储后端：%s", store.backend)
    return store


def reset_vector_store() -> None:
    global _store, _store_key
    _store = None
    _store_key = ""


__all__ = [
    "VectorHit",
    "VectorRow",
    "VectorStore",
    "decode_vector",
    "encode_vector",
    "get_vector_store",
    "reset_vector_store",
]
