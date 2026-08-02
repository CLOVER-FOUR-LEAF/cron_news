from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.category import CategoryCreate, CategoryUpdate, CategoryResponse, CategoryListResponse
from app.crud import category as category_crud

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=CategoryListResponse)
async def list_categories(db: AsyncSession = Depends(get_db)):
    items, total = await category_crud.get_category_list(db)
    return CategoryListResponse(total=total, items=items)


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(category_id: int, db: AsyncSession = Depends(get_db)):
    category = await category_crud.get_category_by_id(db, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="分类不存在")
    return category


@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(category_in: CategoryCreate, db: AsyncSession = Depends(get_db)):
    existing = await category_crud.get_category_by_name(db, category_in.name)
    if existing:
        raise HTTPException(status_code=400, detail="分类名称已存在")
    try:
        category = await category_crud.create_category(db, category_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return category


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(category_id: int, category_in: CategoryUpdate, db: AsyncSession = Depends(get_db)):
    if category_in.name:
        existing = await category_crud.get_category_by_name(db, category_in.name)
        if existing and existing.id != category_id:
            raise HTTPException(status_code=400, detail="分类名称已存在")
    try:
        category = await category_crud.update_category(db, category_id, category_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not category:
        raise HTTPException(status_code=404, detail="分类不存在")
    return category


@router.delete("/{category_id}")
async def delete_category(category_id: int, db: AsyncSession = Depends(get_db)):
    try:
        success = await category_crud.delete_category(db, category_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="分类不存在")
    return {"message": "ok"}
