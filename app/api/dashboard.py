from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date
from app.core.db import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# 食材分类映射（基于 Day 4 固定种子数据）
_CATEGORY_MAP: Dict[str, str] = {
    "东北大米": "主食",
    "面粉": "主食",
    "优质猪肉": "生鲜肉类",
    "鲜嫩鸡胸肉": "生鲜肉类",
    "原切雪花牛肉": "生鲜肉类",
    "冰鲜基围虾": "水产海鲜",
    "有机菜心": "蔬菜时蔬",
    "高山土豆": "蔬菜时蔬",
    "非转基因大豆油": "调料",
    "招牌特调酱油": "调料",
}


def _health(ratio: float) -> str:
    if ratio > 150:
        return "healthy"
    if ratio >= 100:
        return "low"
    return "critical"


@router.get("")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    inv_result = await db.execute(
        text(
            """
            SELECT i.id, i.name, i.unit, inv.current_stock, inv.safety_stock, inv.daily_sales
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            ORDER BY i.id
            """
        )
    )

    inventory: List[Dict[str, Any]] = []
    for row in inv_result.fetchall():
        ing_id, name, unit, current_stock, safety_stock, daily_sales = row
        current_stock = float(current_stock)
        safety_stock = float(safety_stock)
        ratio = (current_stock / safety_stock * 100) if safety_stock > 0 else 0.0
        inventory.append(
            {
                "ingredient_id": ing_id,
                "name": name,
                "unit": unit,
                "category": _CATEGORY_MAP.get(name, "其他"),
                "current_stock": current_stock,
                "safety_stock": safety_stock,
                "daily_sales": float(daily_sales),
                "health": _health(ratio),
                "ratio": round(ratio, 1),
            }
        )

    order_result = await db.execute(
        text(
            """
            SELECT po.id, po.order_no, po.status, po.total_amount,
                   po.risk_reason, po.risk_analysis_report,
                   poi.ingredient_id, poi.quantity
            FROM purchase_orders po
            LEFT JOIN purchase_order_items poi ON poi.order_id = po.id
            ORDER BY po.id DESC
            """
        )
    )

    name_map = {row["ingredient_id"]: row["name"] for row in (
        await db.execute(text("SELECT id AS ingredient_id, name FROM ingredients"))
    ).mappings()}

    orders: List[Dict[str, Any]] = []
    for row in order_result.mappings():
        ingredient_id = row["ingredient_id"]
        orders.append(
            {
                "order_id": row["id"],
                "order_no": row["order_no"],
                "ingredient": name_map.get(ingredient_id, "未知") if ingredient_id else None,
                "quantity": float(row["quantity"]) if row["quantity"] else None,
                "total_amount": float(row["total_amount"]) if row["total_amount"] else 0.0,
                "status": row["status"],
                "risk_reason": row["risk_reason"],
                "risk_analysis_report": row["risk_analysis_report"],
            }
        )

    return {
        "virtual_date": get_current_date().isoformat(),
        "inventory": inventory,
        "orders": orders,
    }
