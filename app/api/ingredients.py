from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db

router = APIRouter(prefix="/ingredients", tags=["ingredients"])


class IngredientCreate(BaseModel):
    name: str = Field(..., description="食材名称", min_length=1)
    category: Optional[str] = Field(default=None, description="分类，如：生鲜肉类/蔬菜时蔬/主食/调料")
    current_stock: float = Field(default=0.0, ge=0, description="当前库存")
    safety_stock: float = Field(default=0.0, ge=0, description="安全线库存")
    daily_consumption: float = Field(default=0.0, ge=0, description="每日消耗量")
    unit_price: float = Field(default=0.0, ge=0, description="当前单价")
    historical_price: Optional[float] = Field(default=None, description="历史均价；缺省按现价 80% 估算，便于触发风控 HITL")


@router.post("")
async def create_ingredient(
    request: IngredientCreate,
    db: AsyncSession = Depends(get_db),
):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")

    # 查重
    exists = (await db.execute(text("SELECT id FROM ingredients WHERE name = :name LIMIT 1"), {"name": name})).first()
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"食材「{name}」已存在")

    unit = "kg"
    historical = request.historical_price
    if historical is None:
        historical = round(request.unit_price * 0.8, 2)

    try:
        await db.execute(
            text(
                """
                INSERT INTO ingredients (name, unit, category)
                VALUES (:name, :unit, :category)
                """
            ),
            {"name": name, "unit": unit, "category": request.category},
        )
        ing_id = int((await db.execute(text("SELECT id FROM ingredients WHERE name = :name"), {"name": name})).scalar_one())

        await db.execute(
            text(
                """
                INSERT INTO inventory (ingredient_id, current_stock, daily_sales, safety_stock)
                VALUES (:ingredient_id, :current_stock, :daily_sales, :safety_stock)
                """
            ),
            {
                "ingredient_id": ing_id,
                "current_stock": request.current_stock,
                "daily_sales": request.daily_consumption,
                "safety_stock": request.safety_stock,
            },
        )

        await db.execute(
            text(
                """
                INSERT INTO suppliers (name, ingredient_id, current_price, historical_avg_price, rating)
                VALUES (:name, :ingredient_id, :current_price, :historical_avg_price, :rating)
                """
            ),
            {
                "name": f"{name}供应商",
                "ingredient_id": ing_id,
                "current_price": request.unit_price,
                "historical_avg_price": historical,
                "rating": 4.50,
            },
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create ingredient: {exc}")

    return {
        "ingredient_id": ing_id,
        "name": name,
        "category": request.category,
        "unit": unit,
        "current_stock": request.current_stock,
        "safety_stock": request.safety_stock,
        "daily_consumption": request.daily_consumption,
        "unit_price": request.unit_price,
        "historical_avg_price": historical,
    }
