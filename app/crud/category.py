import random

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.news import News
from app.schemas.category import CategoryCreate, CategoryUpdate

COLOR_POOL = [
    "#56b3a6", "#5b8ff9", "#e87591", "#9a7ce8", "#63c5e8",
    "#7cb86a", "#e8b45a", "#e8956b", "#6bd4c0", "#c97bb8",
    "#6b8ce8", "#a8c256", "#e87070", "#8a9de8", "#5ac8b8",
    "#d49a6a",
]

DEFAULT_COLOR = "#8a8690"
MAX_CATEGORIES = 10


async def pick_unused_color(db: AsyncSession) -> str:
    result = await db.execute(select(Category.color).where(Category.color.isnot(None)))
    used = {row[0] for row in result.all()}
    available = [c for c in COLOR_POOL if c not in used]
    return random.choice(available) if available else random.choice(COLOR_POOL)


async def get_category_list(db: AsyncSession) -> tuple[list[Category], int]:
    query = select(Category).order_by(Category.sort_order.desc(), Category.id)
    result = await db.execute(query)
    items = result.scalars().all()
    return items, len(items)


async def get_category_by_id(db: AsyncSession, category_id: int) -> Category | None:
    query = select(Category).where(Category.id == category_id)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_category_by_name(db: AsyncSession, name: str) -> Category | None:
    query = select(Category).where(Category.name == name)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_default_category(db: AsyncSession) -> Category | None:
    query = select(Category).where(Category.is_default == True)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def create_category(db: AsyncSession, category_in: CategoryCreate) -> Category:
    result = await db.execute(select(func.count()).select_from(Category))
    if (result.scalar() or 0) >= MAX_CATEGORIES:
        raise ValueError(f"分类数量已达上限（最多 {MAX_CATEGORIES} 个）")

    color = category_in.color
    if not color:
        color = await pick_unused_color(db)

    sort_order = category_in.sort_order
    if not sort_order:
        result = await db.execute(select(func.max(Category.sort_order)))
        sort_order = (result.scalar() or 0) + 1

    category = Category(
        name=category_in.name,
        sort_order=sort_order,
        color=color,
    )
    db.add(category)
    await db.flush()
    await db.refresh(category)
    return category


async def update_category(db: AsyncSession, category_id: int, category_in: CategoryUpdate) -> Category | None:
    category = await get_category_by_id(db, category_id)
    if not category:
        return None
    if category.is_default:
        raise ValueError("默认分类不可修改")
    if category_in.name is not None:
        category.name = category_in.name
    if category_in.sort_order is not None:
        category.sort_order = category_in.sort_order
    await db.flush()
    await db.refresh(category)
    return category


async def delete_category(db: AsyncSession, category_id: int) -> bool:
    category = await get_category_by_id(db, category_id)
    if not category:
        return False
    if category.is_default:
        raise ValueError("默认分类不可删除")

    default_category = await get_default_category(db)
    if not default_category:
        raise ValueError("未找到默认分类")

    if category.id != default_category.id:
        update_query = (
            News.__table__.update()
            .where(News.category_id == category.id)
            .values(category_id=default_category.id)
        )
        await db.execute(update_query)

    await db.delete(category)
    await db.flush()
    return True
