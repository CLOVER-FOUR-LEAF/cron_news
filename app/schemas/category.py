from datetime import datetime

from pydantic import BaseModel, Field


class CategoryBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="分类名称")
    sort_order: int = Field(0, description="排序权重")
    color: str | None = Field(None, max_length=20, description="主题色")


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=50, description="分类名称")
    sort_order: int | None = Field(None, description="排序权重")


class CategoryResponse(CategoryBase):
    id: int
    is_default: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class CategoryListResponse(BaseModel):
    total: int
    items: list[CategoryResponse]
