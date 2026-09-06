from datetime import date
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date


def compose_inbound_record_no(inbound_date: date, order_item_id: int) -> str:
    """入库单号（Phase 6-B-1 v2.1 冻结方案）。

    由业务日期 + order_item_id(AUTO_INCREMENT 主键) 派生：
    - order_item_id 全局唯一，且 inbound 与该 item 1:1 → record_no 天然唯一；
    - 无 SELECT MAX+1、无计数器、无额外分布式锁；
    - UNIQUE(record_no) 仍为数据库最终兜底。
    示例：INBOUND-20260906-00000023
    """
    return f"INBOUND-{inbound_date.strftime('%Y%m%d')}-{int(order_item_id):08d}"


async def execute_inbound_stock(db: AsyncSession, order_id: int) -> Dict[str, Any]:
    """强绑定的物理入库通用逻辑（Phase 6-B-1 冻结事务模型）。

    单一事务内完成：
        INSERT inbound_records   （本事务第一个写，作为 DB 幂等兜底）
        UPDATE inventory  current_stock += qty
        UPDATE purchase_orders  status='COMPLETED', completed_at
    任一步失败 → ROLLBACK，绝不允许"先入库/先加库存/先完成订单"的分次提交。

    order_id / order_item_id 一致性：只允许从本函数加载的同一个 order_item
    构造 inbound 记录（inbound.order_id == item.order_id），调用方不得分别传入
    两个可能不一致的 ID。

    幂等语义：
    - 守卫①：订单已 COMPLETED → 直接返回（快路径）。
    - 守卫②：撞 UNIQUE(order_item_id) 的 IntegrityError → rollback 后按幂等成功
      返回；其它 IntegrityError → rollback 后继续抛出（绝不吞异常）。
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
        # 已完成的订单，避免重复累加/重复写入库，直接返回当前状态
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "already_completed": True,
            "restocked_quantity": 0.0,
            "current_stock": await _get_stock_for_order(db, order_id),
        }

    # 2. 找到对应食材明细（一单一食材，取唯一行）
    item = (
        await db.execute(
            text(
                """
                SELECT id, ingredient_id, quantity, unit_price, total_price, supplier_id
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

    item_id = int(item["id"])
    ingredient_id = int(item["ingredient_id"])
    quantity = float(item["quantity"])
    unit_price = float(item["unit_price"] or 0)
    total_price = float(item["total_price"] if item["total_price"] is not None else round(quantity * unit_price, 2))
    supplier_id = int(item["supplier_id"])
    inbound_date = get_current_date()
    record_no = compose_inbound_record_no(inbound_date, item_id)

    # 3. 单一事务：入库流水(先写) + 库存累加 + 订单置 COMPLETED
    try:
        await db.execute(
            text(
                """
                INSERT INTO inbound_records
                    (record_no, order_id, order_item_id, ingredient_id,
                     inbound_qty, unit_price, total_price, supplier_id, inbound_virtual_date)
                VALUES
                    (:record_no, :order_id, :order_item_id, :ingredient_id,
                     :inbound_qty, :unit_price, :total_price, :supplier_id, :inbound_virtual_date)
                """
            ),
            {
                "record_no": record_no,
                "order_id": order_id,
                "order_item_id": item_id,
                "ingredient_id": ingredient_id,
                "inbound_qty": quantity,
                "unit_price": unit_price,
                "total_price": total_price,
                "supplier_id": supplier_id,
                "inbound_virtual_date": inbound_date.isoformat(),
            },
        )
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
        await db.execute(
            text(
                """
                UPDATE purchase_orders
                SET status = 'COMPLETED', completed_at = :completed_at
                WHERE id = :order_id
                """
            ),
            {"status": "COMPLETED", "completed_at": inbound_date.isoformat(), "order_id": order_id},
        )
        await db.commit()
    except (IntegrityError, OperationalError) as exc:
        # 先让 session 脱离 failed transaction 状态，再判断。
        # 注意：MySQL CHECK 违反(3819)经 aiomysql 映射为 OperationalError，
        # 唯一键 1062 才是 IntegrityError —— 两者都先 rollback，绝不吞异常。
        await db.rollback()
        if not _is_order_item_unique_conflict(exc):
            raise
        # 命中 uq_inbound_order_item：并发/重复执行下该明细已产生过入库 → 幂等成功
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "already_completed": True,
            "restocked_quantity": 0.0,
            "current_stock": await _get_stock_for_order(db, order_id),
        }

    latest = await _get_stock_for_order(db, order_id)

    return {
        "order_id": order_id,
        "order_no": order["order_no"],
        "thread_id": order["thread_id"],
        "status": "COMPLETED",
        "ingredient_id": ingredient_id,
        "restocked_quantity": quantity,
        "current_stock": latest,
        "inbound_record_no": record_no,
    }


def _is_order_item_unique_conflict(exc: Exception) -> bool:
    """只把『同一明细重复入库』的唯一冲突识别为幂等；其余一律 False。

    校验口径：MySQL 1062 duplicate-entry，且约束名命中
    'uq_inbound_order_item' 或 'uq_inbound_record_no'。
    record_no = INBOUND-<date>-<order_item_id>，与 order_item_id 一一对应，
    因此撞 record_no 唯一必然意味着同一 order_item 已入库 —— 两个唯一约束
    是同一场景的等价信号。FK / NOT NULL / CHECK 等其它错误绝不在此被吞。
    """
    msg = ""
    for piece in exc.orig.args if getattr(exc.orig, "args", None) else ():
        if isinstance(piece, bytes):
            piece = piece.decode("utf-8", "replace")
        msg += str(piece)
    return "1062" in msg and (
        "uq_inbound_order_item" in msg or "uq_inbound_record_no" in msg
    )


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
