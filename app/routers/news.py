from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
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


@router.post("/news/{news_id}/reading")
async def mark_as_reading(
    news_id: int,
    db: AsyncSession = Depends(get_db),
):
    success = await news_crud.mark_news_as_reading(db, news_id)
    if not success:
        raise HTTPException(status_code=404, detail="新闻不存在")
    return {"message": "ok"}


@router.post("/news/{news_id}/fav")
async def toggle_fav(
    news_id: int,
    db: AsyncSession = Depends(get_db),
):
    value = await news_crud.toggle_news_flag(db, news_id, "fav")
    if value is None:
        raise HTTPException(status_code=404, detail="新闻不存在")
    return {"is_fav": value}


@router.post("/news/{news_id}/later")
async def toggle_later(
    news_id: int,
    db: AsyncSession = Depends(get_db),
):
    value = await news_crud.toggle_news_flag(db, news_id, "later")
    if value is None:
        raise HTTPException(status_code=404, detail="新闻不存在")
    return {"is_later": value}


@router.get("/my/favorites", response_model=NewsListResponse)
async def my_favorites(db: AsyncSession = Depends(get_db)):
    items = await news_crud.get_my_list(db, "favorites")
    return NewsListResponse(total=len(items), items=items)


@router.get("/my/later", response_model=NewsListResponse)
async def my_later(db: AsyncSession = Depends(get_db)):
    items = await news_crud.get_my_list(db, "later")
    return NewsListResponse(total=len(items), items=items)


@router.get("/my/counts")
async def my_counts(db: AsyncSession = Depends(get_db)):
    return await news_crud.get_my_counts(db)


class BriefNoteModel(BaseModel):
    category: str
    date: str
    content: str = ""


@router.get("/brief/note", response_model=None)
async def get_brief_note(
    category: str = Query(...),
    date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    from app.models import Brief

    result = await db.execute(
        select(Brief).where(Brief.category_name == category, Brief.brief_date == date)
    )
    brief = result.scalars().first()
    return {"content": brief.note if brief else ""}


@router.put("/brief/note")
async def save_brief_note(body: BriefNoteModel, db: AsyncSession = Depends(get_db)):
    from app.models import Brief

    result = await db.execute(
        select(Brief).where(Brief.category_name == body.category, Brief.brief_date == body.date)
    )
    brief = result.scalars().first()
    if brief:
        brief.note = body.content
    else:
        brief = Brief(category_name=body.category, brief_date=body.date, content="", note=body.content)
        db.add(brief)
    await db.flush()
    return {"ok": True}


@router.get("/brief/dates/{category}")
async def get_brief_dates(
    category: str,
    db: AsyncSession = Depends(get_db),
):
    from app.services import brief_service

    return {"dates": await brief_service.get_brief_dates(db, category)}


@router.get("/brief/{category}")
async def get_brief(
    category: str,
    date: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    from app.services import brief_service

    if date:
        brief = await brief_service.get_brief_by_date(db, category, date)
    else:
        brief = await brief_service.get_latest_brief(db, category)
    if not brief:
        raise HTTPException(status_code=404, detail="暂无该分类的简报")
    return {
        "category": brief.category_name,
        "date": brief.brief_date,
        "content": brief.content,
        "source": brief.source,
        "note": brief.note,
    }


class BriefCreateModel(BaseModel):
    category: str
    date: str
    content: str = ""
    note: str | None = None
    source: str = "外部"


@router.post("/brief", status_code=201)
async def create_brief(body: BriefCreateModel, db: AsyncSession = Depends(get_db)):
    """供外部 Agent 写入每日简报（辅助模式下通常由外部 Agent 调用）。"""
    from app.models import Brief

    source = "外部" if body.source not in ("自主", "外部", "用户") else body.source
    result = await db.execute(
        select(Brief).where(Brief.category_name == body.category, Brief.brief_date == body.date)
    )
    brief = result.scalars().first()
    if brief:
        brief.content = body.content
        if body.note is not None:
            brief.note = body.note
        brief.source = source
    else:
        brief = Brief(
            category_name=body.category,
            brief_date=body.date,
            content=body.content,
            note=body.note or "",
            source=source,
        )
        db.add(brief)
    await db.flush()
    return {
        "category": brief.category_name,
        "date": brief.brief_date,
        "content": brief.content,
        "source": brief.source,
        "note": brief.note or "",
    }


@router.put("/brief")
async def update_brief_editor(body: BriefCreateModel, db: AsyncSession = Depends(get_db)):
    """用户编辑简报正文（简报页中间区域），自动保存。

    - 每日简报功能关闭时，用户撰写的内容来源标记为「用户」；
    - 功能开启时，保留既有来源（自主/外部），新建内容按「用户」记录。
    """
    from app.models import Brief

    enabled = read_env_file().get("AGENT_BRIEF_ENABLED", "") == "true"
    result = await db.execute(
        select(Brief).where(Brief.category_name == body.category, Brief.brief_date == body.date)
    )
    brief = result.scalars().first()
    if brief:
        brief.content = body.content
        if body.note is not None:
            brief.note = body.note
        if not enabled:
            brief.source = "用户"
    else:
        brief = Brief(
            category_name=body.category,
            brief_date=body.date,
            content=body.content,
            note=body.note or "",
            source="用户",
        )
        db.add(brief)
    await db.flush()
    return {
        "category": brief.category_name,
        "date": brief.brief_date,
        "content": brief.content,
        "source": brief.source,
        "note": brief.note or "",
    }


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


@router.get("/stats/recommend")
async def get_recommendations(db: AsyncSession = Depends(get_db)):
    mode = read_env_file().get("WORK_MODE", "")
    return await news_crud.get_recommended_news(db, mode)
