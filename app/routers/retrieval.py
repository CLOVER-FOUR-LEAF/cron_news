"""只读检索 API：供聊天页、调试和外部 Agent 使用。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine, get_db
from app.services import retrieval
from app.services.vector_store import get_vector_store

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(8, ge=1, le=20),
    category: str | None = Query(None),
    days: int | None = Query(None, ge=1, le=365),
    mode: str = Query("hybrid", pattern="^(hybrid|lexical|semantic)$"),
    db: AsyncSession = Depends(get_db),
):
    return await retrieval.search_news(
        db, q, category=category, days=days, limit=limit, mode=mode
    )


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    data = await retrieval.status(db)
    store = get_vector_store(engine)
    data["vector_backend"] = store.backend
    return data


@router.post("/reindex")
async def reindex(limit: int = Query(200, ge=1, le=5000)):
    """补缺失/过期的新闻向量。配置了 embedding 模型后调用一次即可。"""
    from app.services import embeddings

    return await embeddings.backfill(limit=limit)


class ProbeModel(BaseModel):
    q: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(8, ge=1, le=20)


@router.post("/probe")
async def probe(body: ProbeModel, db: AsyncSession = Depends(get_db)):
    """检索调试：同时返回词法、语义、融合三组结果，便于评估召回。"""
    hybrid = await retrieval.search_news(db, body.q, limit=body.limit)
    lexical = await retrieval.search_news(db, body.q, limit=body.limit, mode="lexical")
    semantic = await retrieval.search_news(db, body.q, limit=body.limit, mode="semantic")
    return {
        "query": body.q,
        "hybrid": hybrid,
        "lexical": lexical,
        "semantic": semantic,
    }


@router.post("/vector/rebuild")
async def rebuild_vector_index():
    store = get_vector_store(engine)
    try:
        return await store.rebuild_index()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"重建失败：{exc}") from exc


@router.post("/vector/ann-index")
async def build_ann_index(dim: int = Query(..., ge=1, le=4096)):
    store = get_vector_store(engine)
    if not hasattr(store, "create_ann_index"):
        raise HTTPException(status_code=400, detail="当前向量后端不支持 ANN 索引")
    return await store.create_ann_index(dim)
