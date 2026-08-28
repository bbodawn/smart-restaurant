import uuid
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.state import PurchaseState

# 未完成采购单状态（用于避免重复触发）
_PENDING_STATUSES = ("PENDING", "SUSPENDED", "PURCHASE_CREATED", "RUNNING")


async def _load_ingredient_bundle(db: AsyncSession, name: str) -> Dict[str, Any] | None:
    """加载食材 + 库存 + 供应商信息快照。"""
    result = await db.execute(
        text(
            """
            SELECT
                i.id AS ingredient_id,
                i.name AS ingredient_name,
                i.unit,
                inv.current_stock,
                inv.daily_sales,
                inv.safety_stock,
                s.id AS supplier_id,
                s.name AS supplier_name,
                s.current_price,
                s.historical_avg_price
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            JOIN suppliers s ON s.ingredient_id = i.id
            WHERE i.name = :name
            LIMIT 1
            """
        ),
        {"name": name},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return dict(row)


async def _has_open_order(db: AsyncSession, ingredient_id: int) -> bool:
    """判断该食材是否已存在未完成的采购单。"""
    result = await db.execute(
        text(
            """
            SELECT po.id
            FROM purchase_orders po
            JOIN purchase_order_items poi ON poi.order_id = po.id
            WHERE poi.ingredient_id = :ingredient_id
              AND po.status IN :statuses
            LIMIT 1
            """
        ),
        {"ingredient_id": ingredient_id, "statuses": _PENDING_STATUSES},
    )
    return result.first() is not None


async def scan_and_trigger_procurement(
    db: AsyncSession, graph: Any
) -> List[Dict[str, Any]]:
    """巡检所有低于安全库存线的食材，为缺货食材自动触发采购工作流。

    返回本次自动创建的采购单信息列表。
    """
    result = await db.execute(
        text(
            """
            SELECT i.name
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            WHERE inv.current_stock <= inv.safety_stock
            """
        )
    )
    low_stock_ingredients = [row[0] for row in result.fetchall()]

    triggered: List[Dict[str, Any]] = []
    for name in low_stock_ingredients:
        bundle = await _load_ingredient_bundle(db, name)
        if bundle is None:
            continue

        if await _has_open_order(db, bundle["ingredient_id"]):
            continue

        order_no = "PO-AUTO-" + uuid.uuid4().hex[:12].upper()
        thread_id = "auto-" + uuid.uuid4().hex

        await db.execute(
            text(
                """
                INSERT INTO purchase_orders (order_no, thread_id, status)
                VALUES (:order_no, :thread_id, 'RUNNING')
                """
            ),
            {"order_no": order_no, "thread_id": thread_id},
        )
        db_result = await db.execute(
            text("SELECT id FROM purchase_orders WHERE order_no = :order_no"),
            {"order_no": order_no},
        )
        order_id = int(db_result.scalar_one())

        graph_state: PurchaseState = {
            "order_id": order_id,
            "order_no": order_no,
            "thread_id": thread_id,
            "ingredient_id": int(bundle["ingredient_id"]),
            "ingredient": bundle["ingredient_name"],
            "unit": bundle["unit"],
            "current_stock": float(bundle["current_stock"]),
            "daily_sales": float(bundle["daily_sales"]),
            "predicted_demand": 0.0,
            "quantity": 0.0,
            "demand_reasoning": "",
            "supplier_id": int(bundle["supplier_id"]),
            "supplier_name": bundle["supplier_name"],
            "supplier_price": float(bundle["current_price"]),
            "historical_price": float(bundle["historical_avg_price"]),
            "price_deviation": 0.0,
            "total_amount": 0.0,
            "risk_analysis_report": None,
            "risk_reason": None,
            "approved": None,
            "status": "RUNNING",
        }

        config = {"configurable": {"thread_id": thread_id}}
        run_result = await graph.ainvoke(graph_state, config=config)

        if "__interrupt__" in run_result:
            interrupt_items = run_result["__interrupt__"]
            interrupt_value: Dict[str, Any] = {}
            if interrupt_items:
                interrupt_value = interrupt_items[0].value

            quantity = float(interrupt_value.get("quantity", 0))
            supplier_price = float(interrupt_value.get("supplier_price", 0))
            total_amount = float(interrupt_value.get("total_amount", 0))
            risk_analysis_report = interrupt_value.get("risk_analysis_report")
            if not risk_analysis_report:
                risk_analysis_report = interrupt_value.get("risk_reason")

            await db.execute(
                text(
                    """
                    UPDATE purchase_orders
                    SET status = 'SUSPENDED', total_amount = :total_amount,
                        risk_analysis_report = :risk_analysis_report,
                        demand_reasoning = :demand_reasoning
                    WHERE id = :order_id
                    """
                ),
                {
                    "total_amount": total_amount,
                    "risk_analysis_report": risk_analysis_report,
                    "demand_reasoning": run_result.get("demand_reasoning", ""),
                    "order_id": order_id,
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO purchase_order_items
                        (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                    VALUES
                        (:order_id, :ingredient_id, :quantity, :unit_price, :total_price, :supplier_id)
                    """
                ),
                {
                    "order_id": order_id,
                    "ingredient_id": int(bundle["ingredient_id"]),
                    "quantity": quantity,
                    "unit_price": supplier_price,
                    "total_price": total_amount,
                    "supplier_id": int(bundle["supplier_id"]),
                },
            )
            await db.commit()

            triggered.append(
                {
                    "order_id": order_id,
                    "ingredient": bundle["ingredient_name"],
                    "status": "SUSPENDED",
                    "quantity": quantity,
                    "demand_reasoning": run_result.get("demand_reasoning", ""),
                    "risk_analysis_report": risk_analysis_report,
                }
            )
            continue

        final_status = run_result.get("status", "UNKNOWN")
        quantity_auto = float(run_result.get("quantity", 0))
        total_amount_auto = float(run_result.get("total_amount", 0))

        # 非中断路径（NO_PURCHASE/PURCHASE_CREATED 等）也记录采购明细，供看板展示
        if quantity_auto > 0:
            await db.execute(
                text(
                    """
                    INSERT INTO purchase_order_items
                        (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                    VALUES
                        (:order_id, :ingredient_id, :quantity, :unit_price, :total_price, :supplier_id)
                    """
                ),
                {
                    "order_id": order_id,
                    "ingredient_id": int(bundle["ingredient_id"]),
                    "quantity": quantity_auto,
                    "unit_price": float(bundle["current_price"]),
                    "total_price": total_amount_auto,
                    "supplier_id": int(bundle["supplier_id"]),
                },
            )

        await db.execute(
            text(
                """
                UPDATE purchase_orders
                SET status = :status, total_amount = :total_amount,
                    demand_reasoning = :demand_reasoning
                WHERE id = :order_id
                """
            ),
            {
                "status": final_status,
                "total_amount": total_amount_auto,
                "demand_reasoning": run_result.get("demand_reasoning", ""),
                "order_id": order_id,
            },
        )
        await db.commit()

        triggered.append(
            {
                "order_id": order_id,
                "ingredient": bundle["ingredient_name"],
                "status": final_status,
                "quantity": quantity_auto,
                "demand_reasoning": run_result.get("demand_reasoning", ""),
                "risk_analysis_report": run_result.get("risk_analysis_report"),
            }
        )

    return triggered
