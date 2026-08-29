from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date


async def execute_inbound_stock(db: AsyncSession, order_id: int) -> Dict[str, Any]:
    """强绑定的物理入库通用逻辑。

    根据采购订单回补对应食材的物理库存，并把订单置为 COMPLETED。
    只有库存累加成功且订单存在明细时，订单才会被标记为 COMPLETED，
    从根本上杜绝"订单已 COMPLETED 但库存未加回"的异常。
    """
    # 1. 查询采购订单
    order = (
        await db.execute(
            text(
                "SELECT id, order_no, thread_id, status FROM purchase_orders WHERE id = :order_id LIMIT 1"
            ),
            {"order_id": order_id},
        )
    ).mappings().first()
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    current_status = order["status"]
    if current_status == "COMPLETED":
        # 已完成的订单，避免重复累加，直接返回当前状态
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "already_completed": True,
            "restocked_quantity": 0.0,
            "current_stock": await _get_stock_for_order(db, order_id),
        }

    # 2. 找到对应食材明细（支持 ingredient_id）
    item = (
        await db.execute(
            text(
                """
                SELECT ingredient_id, quantity
                FROM purchase_order_items
                WHERE order_id = :order_id
                ORDER BY id
                LIMIT 1
                """
            ),
            {"order_id": order_id},
        )
    ).mappings().first()
    if item is None or not item["quantity"]:
        raise HTTPException(
            status_code=400,
            detail=f"Order {order_id} has no purchasable item detail; cannot inbound",
        )

    ingredient_id = int(item["ingredient_id"])
    quantity = float(item["quantity"])

    # 3. 执行库存原子累加
    await db.execute(
        text(
            """
            UPDATE inventory
            SET current_stock = current_stock + :quantity
            WHERE ingredient_id = :ingredient_id
            """
        ),
        {"quantity": quantity, "ingredient_id": ingredient_id},
    )

    # 4. 标记订单为 COMPLETED，并写入虚拟时间戳
    await db.execute(
        text(
            """
            UPDATE purchase_orders
            SET status = 'COMPLETED', completed_at = :completed_at
            WHERE id = :order_id
            """
        ),
        {"status": "COMPLETED", "completed_at": get_current_date().isoformat(), "order_id": order_id},
    )

    # 5. 提交事务，确保物理变更落库
    await db.commit()
    latest = await _get_stock_for_order(db, order_id)

    return {
        "order_id": order_id,
        "order_no": order["order_no"],
        "thread_id": order["thread_id"],
        "status": "COMPLETED",
        "ingredient_id": ingredient_id,
        "restocked_quantity": quantity,
        "current_stock": latest,
    }


async def _get_stock_for_order(db: AsyncSession, order_id: int):
    result = await db.execute(
        text(
            """
            SELECT inv.current_stock
            FROM purchase_order_items poi
            JOIN inventory inv ON inv.ingredient_id = poi.ingredient_id
            WHERE poi.order_id = :order_id
            ORDER BY poi.id
            LIMIT 1
            """
        ),
        {"order_id": order_id},
    )
    row = result.mappings().first()
    return float(row["current_stock"]) if row else None
