from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.news import NewsCreate, NewsResponse, NewsListResponse
from app.crud import news as news_crud
from app.env_store import read_env_file

router = APIRouter(prefix="/api", tags=["news"])


@router.get("/news", response_model=NewsListResponse)
async def list_news(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
    keyword: str | None = Query(None, description="搜索关键词"),
    category: str | None = Query(None, description="分类名称筛选"),
    today_only: bool = Query(False, description="仅显示今日新闻"),
    date_filter: date | None = Query(None, description="按日期筛选(YYYY-MM-DD)"),
    is_read: int | None = Query(None, description="已读状态筛选(0/1)"),
    db: AsyncSession = Depends(get_db),
):
    items, total = await news_crud.get_news_list(db, page, page_size, keyword, category, today_only, date_filter, is_read)
    return NewsListResponse(total=total, items=items)


@router.get("/news/{news_id}", response_model=NewsResponse)
async def get_news(
    news_id: int,
    db: AsyncSession = Depends(get_db),
):
    news = await news_crud.get_news_by_id(db, news_id)
    if not news:
        raise HTTPException(status_code=404, detail="新闻不存在")
    return news


@router.get("/news/{news_id}/related", response_model=NewsListResponse)
async def get_related_news(
    news_id: int,
    limit: int = Query(10, ge=1, le=30, description="推荐数量"),
    db: AsyncSession = Depends(get_db),
):
    recommend_enabled = read_env_file().get("AGENT_RECOMMEND_ENABLED", "") == "true"
    items = await news_crud.get_related_news(db, news_id, limit, recommend_enabled)
    return NewsListResponse(total=len(items), items=items)


@router.post("/news/{news_id}/read")
async def mark_as_read(
    news_id: int,
    db: AsyncSession = Depends(get_db),
):
    success = await news_crud.mark_news_as_read(db, news_id)
    if not success:
        raise HTTPException(status_code=404, detail="新闻不存在")
    return {"message": "ok"}


@router.post("/news", response_model=NewsResponse, status_code=201)
async def create_news(
    news_in: NewsCreate,
    db: AsyncSession = Depends(get_db),
):
    news = await news_crud.create_news(db, news_in)
    return news


@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    stats = await news_crud.get_news_stats(db)
    return stats
