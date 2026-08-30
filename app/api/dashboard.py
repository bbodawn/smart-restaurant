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
    # ---------- 库存看板 ----------
    inv_result = await db.execute(
        text(
            """
            SELECT i.id, i.name, i.unit, i.category, inv.current_stock, inv.safety_stock, inv.daily_sales,
                   s.current_price AS price
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            JOIN suppliers s ON s.ingredient_id = i.id
            ORDER BY i.id
            """
        )
    )

    inventory: List[Dict[str, Any]] = []
    for row in inv_result.fetchall():
        ing_id, name, unit, category, current_stock, safety_stock, daily_sales, price = row
        current_stock = float(current_stock)
        safety_stock = float(safety_stock)
        ratio = (current_stock / safety_stock * 100) if safety_stock > 0 else 0.0
        inventory.append(
            {
                "ingredient_id": ing_id,
                "name": name,
                "unit": unit,
                "category": category or _CATEGORY_MAP.get(name, "其他"),
                "current_stock": current_stock,
                "safety_stock": safety_stock,
                "daily_sales": float(daily_sales),
                "price": float(price) if price is not None else None,
                "health": _health(ratio),
                "ratio": round(ratio, 1),
            }
        )

    # ---------- 采购订单列表 ----------
    order_result = await db.execute(
        text(
            """
            SELECT po.id, po.order_no, po.status, po.total_amount,
                   po.risk_reason, po.risk_analysis_report, po.demand_reasoning,
                   po.created_at, po.updated_at,
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
    total_orders = 0
    auto_completed = 0
    pending_count = 0
    intercepted_count = 0
    month_restock_amount = 0.0
    current_date = get_current_date()
    current_month_prefix = current_date.strftime("%Y-%m")

    for row in order_result.mappings():
        total_orders += 1
        status = row["status"]
        total_amount = float(row["total_amount"]) if row["total_amount"] else 0.0
        updated_at = row["updated_at"]

        if status in ("COMPLETED", "PURCHASE_CREATED"):
            auto_completed += 1
        if status == "SUSPENDED":
            pending_count += 1
        if row["risk_analysis_report"] or row["risk_reason"]:
            intercepted_count += 1
        if status == "COMPLETED":
            if updated_at and updated_at.strftime("%Y-%m") == current_month_prefix:
                month_restock_amount += total_amount

        orders.append(
            {
                "order_id": row["id"],
                "order_no": row["order_no"],
                "ingredient": name_map.get(row["ingredient_id"], "未知") if row["ingredient_id"] else None,
                "quantity": float(row["quantity"]) if row["quantity"] else None,
                "total_amount": total_amount,
                "status": status,
                "risk_reason": row["risk_reason"],
                "risk_analysis_report": row["risk_analysis_report"],
                "demand_reasoning": row["demand_reasoning"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
        )

    auto_rate = (auto_completed / total_orders * 100) if total_orders > 0 else 0.0

    kpi = {
        "auto_procurement_rate": round(auto_rate, 1),
        "pending_approvals": pending_count,
        "intercepted_risk_orders": intercepted_count,
        "month_restock_amount": round(month_restock_amount, 2),
    }

    return {
        "virtual_date": current_date.isoformat(),
        "inventory": inventory,
        "orders": orders,
        "kpi": kpi,
    }
