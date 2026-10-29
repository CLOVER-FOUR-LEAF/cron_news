"""向量存储抽象：按数据库方言选择实现。

- SQLite / MySQL / MariaDB：`news_embeddings.vector` BLOB + 应用内 numpy 余弦
- PostgreSQL：BLOB 仍然作为事实来源，另外维护 pgvector 派生索引（可重建）

上层 `retrieval` 只依赖本模块的接口，不写任何方言分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

DIM_LIMIT = 4096


@dataclass
class VectorRow:
    news_id: int
    model: str
    dim: int
    vector: bytes
    text_hash: str


@dataclass
class VectorHit:
    news_id: int
    score: float


def encode_vector(values: Sequence[float]) -> bytes:
    """L2 归一化后编码为 float32 little-endian。归一化后余弦相似度就是点积。"""
    array = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    if norm > 0:
        array = array / norm
    return array.astype("<f4").tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


def vector_to_pg_literal(values) -> str:
    array = np.asarray(values, dtype=np.float32)
    return "[" + ",".join(f"{float(item):.7g}" for item in array) + "]"


class VectorStore:
    backend = "blob"
    ann = False

    async def upsert(self, rows: list[VectorRow]) -> int:
        raise NotImplementedError

    async def delete(self, news_ids: list[int]) -> int:
        raise NotImplementedError

    async def search(
        self,
        query_vector: list[float],
        *,
        model: str,
        limit: int,
        tag: str | None = None,
        since=None,
        until=None,
        exclude_ids: set[int] | None = None,
    ) -> list[VectorHit]:
        raise NotImplementedError

    async def stats(self) -> dict:
        raise NotImplementedError

    async def rebuild_index(self) -> dict:
        """从 BLOB 事实来源重建派生索引（PG 用；其它后端无操作）。"""
        return {"rebuilt": 0}
