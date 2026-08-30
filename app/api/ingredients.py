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
    current_stock: float = Field(..., gt=0, description="当前库存（必须大于 0）")
    safety_stock: float = Field(..., gt=0, description="安全线库存（必须大于 0）")
    daily_consumption: float = Field(..., gt=0, description="每日消耗量（必须大于 0）")
    unit_price: float = Field(..., gt=0, description="当前单价（必须大于 0）")
    unit: str = Field(default="kg", description="计量单位，如 kg / L，缺省 kg")
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

    unit = (request.unit or "kg").strip() or "kg"
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


class IngredientUpdate(BaseModel):
    current_stock: Optional[float] = Field(default=None, gt=0, description="当前库存")
    safety_stock: Optional[float] = Field(default=None, gt=0, description="安全线库存")
    daily_consumption: Optional[float] = Field(default=None, gt=0, description="每日消耗量")
    unit_price: Optional[float] = Field(default=None, gt=0, description="采购单价")


@router.put("/{ingredient_id}")
async def update_ingredient(
    ingredient_id: int,
    request: IngredientUpdate,
    db: AsyncSession = Depends(get_db),
):
    ing = (
        await db.execute(text("SELECT id FROM ingredients WHERE id = :id LIMIT 1"), {"id": ingredient_id})
    ).mappings().first()
    if ing is None:
        raise HTTPException(status_code=404, detail=f"食材 id={ingredient_id} 不存在")

    updates = {}
    if request.current_stock is not None:
        updates["current_stock"] = request.current_stock
    if request.safety_stock is not None:
        updates["safety_stock"] = request.safety_stock
    if request.daily_consumption is not None:
        updates["daily_sales"] = request.daily_consumption

    try:
        if updates:
            cols = ", ".join(f"{k} = :{k}" for k in updates)
            await db.execute(
                text(f"UPDATE inventory SET {cols} WHERE ingredient_id = :ingredient_id"),
                {**updates, "ingredient_id": ingredient_id},
            )
        if request.unit_price is not None:
            await db.execute(
                text("UPDATE suppliers SET current_price = :p WHERE ingredient_id = :ingredient_id"),
                {"p": request.unit_price, "ingredient_id": ingredient_id},
            )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update ingredient: {exc}")

    # 返回最新数据
    result = await db.execute(
        text(
            """
            SELECT i.id, i.name, i.unit, i.category, inv.current_stock, inv.safety_stock,
                   inv.daily_sales, s.current_price, s.historical_avg_price
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            JOIN suppliers s ON s.ingredient_id = i.id
            WHERE i.id = :id LIMIT 1
            """
        ),
        {"id": ingredient_id},
    )
    row = result.mappings().first()
    return {
        "ingredient_id": row["id"],
        "name": row["name"],
        "unit": row["unit"],
        "category": row["category"],
        "current_stock": float(row["current_stock"]),
        "safety_stock": float(row["safety_stock"]),
        "daily_consumption": float(row["daily_sales"]),
        "unit_price": float(row["current_price"]),
        "historical_avg_price": float(row["historical_avg_price"]),
    }


@router.delete("/{ingredient_id}")
async def delete_ingredient(
    ingredient_id: int,
    db: AsyncSession = Depends(get_db),
):
    ing = (
        await db.execute(text("SELECT id, name FROM ingredients WHERE id = :id LIMIT 1"), {"id": ingredient_id})
    ).mappings().first()
    if ing is None:
        raise HTTPException(status_code=404, detail=f"食材 id={ingredient_id} 不存在")

    try:
        # 取消关联的未完成采购单（SUSPENDED / PENDING 置为 REJECTED）
        await db.execute(
            text(
                """
                UPDATE purchase_orders po
                JOIN purchase_order_items poi ON poi.order_id = po.id
                SET po.status = 'REJECTED'
                WHERE poi.ingredient_id = :ingredient_id AND po.status IN ('SUSPENDED','PENDING','RUNNING')
                """
            ),
            {"ingredient_id": ingredient_id},
        )
        await db.execute(text("DELETE FROM purchase_order_items WHERE ingredient_id = :id"), {"id": ingredient_id})
        await db.execute(text("DELETE FROM inventory WHERE ingredient_id = :id"), {"id": ingredient_id})
        await db.execute(text("DELETE FROM suppliers WHERE ingredient_id = :id"), {"id": ingredient_id})
        await db.execute(text("DELETE FROM ingredients WHERE id = :id"), {"id": ingredient_id})
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete ingredient: {exc}")

    return {"deleted": True, "ingredient_id": ingredient_id, "name": ing["name"]}

