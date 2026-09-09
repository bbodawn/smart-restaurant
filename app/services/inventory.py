from datetime import date
from typing import Any, Dict, Optional, Sequence, Union

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


class ConsumeItem:
    """库存扣减单元：一个食材的消耗量（qty > 0，按食材自身单位）。"""

    def __init__(self, ingredient_id: int, qty: float):
        if not ingredient_id:
            raise ValueError("ConsumeItem.ingredient_id 不能为空")
        if qty is None or qty <= 0:
            raise ValueError(f"ConsumeItem.qty 必须 > 0（ingredient_id={ingredient_id}）")
        self.ingredient_id = int(ingredient_id)
        self.qty = float(qty)


class InsufficientStock(Exception):
    """库存不足：整单原子拒绝的信号。

    携带缺料清单（ingredient_id/name/unit/required/current），由点单等上层
    业务捕获后映射为 409 + 缺料明细；本异常被抛出时保证零写入。
    """

    def __init__(self, missing: Sequence[dict]):
        self.missing = list(missing)
        parts = [f"{m.get('name', m.get('ingredient_id'))}: 需 {m.get('required')} / 现 {m.get('current')}" for m in self.missing]
        super().__init__("库存不足（整单拒绝）: " + "; ".join(parts))


def _item_ing_id(item: Union[ConsumeItem, dict]) -> int:
    return int(item.ingredient_id if isinstance(item, ConsumeItem) else item["ingredient_id"])


def _item_qty(item: Union[ConsumeItem, dict]) -> float:
    return float(item.qty if isinstance(item, ConsumeItem) else item["qty"])


async def consume_inventory(
    db: AsyncSession,
    items: Sequence[Union[ConsumeItem, dict]],
    *,
    movement_type: str,
    reference_type: str,
    reference_id: int,
    virtual_date: date,
    note: Optional[str] = None,
) -> list[dict]:
    """扣减一组食材库存并逐条写 stock_movements 流水（Phase 10 统一出库入口）。

    整单原子语义：
    - 单事务内：SELECT ... FOR UPDATE 锁行 → 逐个校验足量 → 全部满足才落库；
      任一不足 → 抛 InsufficientStock（含缺料清单），保证零写入（不做部分扣减）。
    - 写序：INSERT stock_movements → UPDATE inventory.current_stock = balance_after。
    - 本函数不自行 commit：由上层业务事务（点单 / 模拟营业）统一提交/回滚，
      以便销售订单头/明细与库存扣减在同一事务内原子完成。

    返回逐食材结果：[{ingredient_id, change_qty, balance_after}]（change_qty 为负）。
    """
    if not items:
        raise ValueError("consume_inventory items 不能为空")
    parsed = [ConsumeItem(_item_ing_id(i), _item_qty(i)) for i in items]

    # 0. 同一食材多行（如多个菜品共用某原料）先聚合：一次业务事件对某食材只扣一次、
    #    只写一条流水。库存快照须按聚合后的总量扣减，避免用同一初始值重复扣/重复记账。
    agg: dict[int, float] = {}
    for it in parsed:
        agg[it.ingredient_id] = agg.get(it.ingredient_id, 0.0) + it.qty
    aggregated = [ConsumeItem(i, q) for i, q in agg.items()]

    # 1. 按 ingredient 锁行读取当前库存（同一事务，防并发双扣）
    ids = [it.ingredient_id for it in aggregated]
    id_marks = ",".join(f":i{n}" for n in range(len(ids)))
    params = {f"i{n}": i for n, i in enumerate(ids)}
    rows = (
        await db.execute(
            text(
                f"""
                SELECT inv.ingredient_id, i.name, i.unit, inv.current_stock
                FROM inventory inv
                JOIN ingredients i ON i.id = inv.ingredient_id
                WHERE inv.ingredient_id IN ({id_marks})
                FOR UPDATE
                """
            ),
            params,
        )
    ).mappings().all()
    stock_map = {r["ingredient_id"]: r for r in rows}

    # 2. 缺料清单（含名称便于上层展示）；任一不足即整单拒绝
    missing = []
    for it in aggregated:
        row = stock_map.get(it.ingredient_id)
        current = float(row["current_stock"]) if row else 0.0
        if row is None or current < it.qty:
            missing.append(
                {
                    "ingredient_id": it.ingredient_id,
                    "name": row["name"] if row else f"ingredient:{it.ingredient_id}",
                    "unit": row["unit"] if row else "",
                    "required": it.qty,
                    "current": current,
                }
            )
    if missing:
        await db.rollback()  # 释放行锁；零写入
        raise InsufficientStock(missing)

    # 3. 写流水 + 更新库存（同一事务，顺序：先流水后库存快照）
    results = []
    for it in aggregated:
        current = float(stock_map[it.ingredient_id]["current_stock"])
        balance_after = round(current - it.qty, 4)
        await db.execute(
            text(
                """
                INSERT INTO stock_movements
                    (ingredient_id, change_qty, balance_after, movement_type,
                     reference_type, reference_id, virtual_date, note)
                VALUES
                    (:ingredient_id, :change_qty, :balance_after, :movement_type,
                     :reference_type, :reference_id, :virtual_date, :note)
                """
            ),
            {
                "ingredient_id": it.ingredient_id,
                "change_qty": -it.qty,
                "balance_after": balance_after,
                "movement_type": movement_type,
                "reference_type": reference_type,
                "reference_id": reference_id,
                "virtual_date": virtual_date.isoformat() if isinstance(virtual_date, date) else virtual_date,
                "note": note,
            },
        )
        await db.execute(
            text(
                """
                UPDATE inventory
                SET current_stock = :new_stock
                WHERE ingredient_id = :ingredient_id
                """
            ),
            {"new_stock": balance_after, "ingredient_id": it.ingredient_id},
        )
        results.append(
            {"ingredient_id": it.ingredient_id, "change_qty": -it.qty, "balance_after": balance_after}
        )
    return results


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
    #    Phase 10：同一事务内追加 stock_movements(INBOUND)，balance_after 与库存一致。
    #    先 FOR UPDATE 锁库存行读当前值 → 计算 new_stock，再写流水与库存快照。
    try:
        inv_row = (
            await db.execute(
                text(
                    "SELECT current_stock FROM inventory WHERE ingredient_id = :ingredient_id FOR UPDATE"
                ),
                {"ingredient_id": ingredient_id},
            )
        ).mappings().first()
        if inv_row is None:
            raise HTTPException(status_code=404, detail="Ingredient inventory not found")
        new_stock = round(float(inv_row["current_stock"]) + quantity, 4)

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
                INSERT INTO stock_movements
                    (ingredient_id, change_qty, balance_after, movement_type,
                     reference_type, reference_id, virtual_date, note)
                VALUES
                    (:ingredient_id, :change_qty, :balance_after, 'INBOUND',
                     'PURCHASE_ORDER', :reference_id, :virtual_date, :note)
                """
            ),
            {
                "ingredient_id": ingredient_id,
                "change_qty": quantity,
                "balance_after": new_stock,
                "reference_id": order_id,
                "virtual_date": inbound_date.isoformat(),
                "note": f"采购入库 {record_no}",
            },
        )
        await db.execute(
            text(
                """
                UPDATE inventory
                SET current_stock = :new_stock
                WHERE ingredient_id = :ingredient_id
                """
            ),
            {"new_stock": new_stock, "ingredient_id": ingredient_id},
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
