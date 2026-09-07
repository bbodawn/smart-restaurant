import uuid
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date
from app.core.lock import acquire_ingredient_lock, release_ingredient_lock
from app.graph.state import PurchaseState
from app.services.inventory import execute_inbound_stock

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
    """巡检当前库存低于“3 日预测需求”线的食材，自动触发采购工作流。

    候选发现范围与 deterministic_policy 的采购触发逻辑对齐：
      predicted_3_day_demand = daily_sales * 3
      current_stock < predicted_3_day_demand  （且 daily_sales > 0）
    落入 candidate 后，由 deterministic_policy 最终裁决为 PURCHASE / REVIEW /
    NO_PURCHASE；其中 REVIEW（触及安全线或价格偏离等）仍走 Agent5+HITL，不自动入库。

    并发保护（Phase 4 Production Hardening）：对每个候选按 ingredient 获取非阻塞
    Redis 锁再加 DB open 检查（三层：ingredient lock + _has_open_order + execute 幂等）。
    lock 只在本次 Auto Procurement 执行覆盖（到 REVIEW commit SUSPENDED / execute 完成），
    **不跨真实 HITL**——人工审批由 approval API 的 order-level lock 另行处理。

    返回本次自动创建的采购单信息列表。
    """
    result = await db.execute(
        text(
            """
            SELECT i.name
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            WHERE inv.daily_sales > 0
              AND inv.current_stock < inv.daily_sales * 3
            """
        )
    )
    low_stock_ingredients = [row[0] for row in result.fetchall()]

    triggered: List[Dict[str, Any]] = []

    async def _process_one(bundle: Dict[str, Any]) -> None:
        """对单个候选执行原采购闭环（在 ingredient lock 内运行）。"""
        # DB 业务状态二次检查（Redis lock 失效时的第二道防线）
        if await _has_open_order(db, bundle["ingredient_id"]):
            return

        order_no = "PO-AUTO-" + uuid.uuid4().hex[:12].upper()
        thread_id = "auto-" + uuid.uuid4().hex

        await db.execute(
            text(
                """
                INSERT INTO purchase_orders (order_no, thread_id, status, source)
                VALUES (:order_no, :thread_id, 'RUNNING', 'AUTO')
                """
            ),
            {"order_no": order_no, "thread_id": thread_id},
        )
        db_result = await db.execute(
            text("SELECT id FROM purchase_orders WHERE order_no = :order_no"),
            {"order_no": order_no},
        )
        order_id = int(db_result.scalar_one())

        # 全量 bundle 作为采购快照（保持原有逻辑不变）
        ingredient_id = int(bundle["ingredient_id"])

        graph_state: PurchaseState = {
            "order_id": order_id,
            "order_no": order_no,
            "thread_id": thread_id,
            "ingredient_id": ingredient_id,
            "ingredient": bundle["ingredient_name"],
            "unit": bundle["unit"],
            "current_stock": float(bundle["current_stock"]),
            "safety_stock": float(bundle["safety_stock"]),
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
            # 用户可读报告：优先取 Agent5 结构化中文(summary/risk_analysis)，
            # 其次取旧链路 risk_analysis_report；绝不拿英文 risk_reason 当文案。
            raw_a5 = interrupt_value.get("agent5_analysis")
            user_parts = []
            if isinstance(raw_a5, dict):
                for k in ("summary", "risk_analysis"):
                    v = raw_a5.get(k)
                    if isinstance(v, str) and v.strip():
                        user_parts.append(v.strip())
            if not user_parts:
                legacy = interrupt_value.get("risk_analysis_report")
                if isinstance(legacy, str) and legacy.strip():
                    user_parts.append(legacy.strip())
            risk_analysis_report = "\n".join(user_parts) or (
                "暂无完整智能风险分析，请结合采购数据进行人工审核。"
            )

            a5d = raw_a5 if isinstance(raw_a5, dict) else {}
            await db.execute(
                text(
                    """
                    UPDATE purchase_orders
                    SET status = 'SUSPENDED', total_amount = :total_amount,
                        risk_analysis_report = :risk_analysis_report,
                        demand_reasoning = :demand_reasoning,
                        suspended_virtual_date = :suspended_virtual_date,
                        agent5_summary = :a5_summary,
                        agent5_risk_level = :a5_risk_level,
                        agent5_risk_analysis = :a5_risk_analysis,
                        agent5_recommendation = :a5_recommendation
                    WHERE id = :order_id
                    """
                ),
                {
                    "total_amount": total_amount,
                    "risk_analysis_report": risk_analysis_report,
                    "demand_reasoning": run_result.get("demand_reasoning", ""),
                    "suspended_virtual_date": get_current_date().isoformat(),
                    "a5_summary": a5d.get("summary"),
                    "a5_risk_level": a5d.get("risk_level"),
                    "a5_risk_analysis": a5d.get("risk_analysis"),
                    "a5_recommendation": a5d.get("recommendation"),
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
                    "ingredient_id": ingredient_id,
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
            return

        pd = run_result.get("policy_decision") or {}
        is_purchase = pd.get("status") == "PURCHASE"
        final_status = run_result.get("status", "UNKNOWN")
        # 采购决策数量以 policy_decision 为准（新链路 quantity 唯一来源在此；
        # 顶层 state["quantity"] 仍为历史默认 0，不能作为入库依据）。
        quantity_auto = float(pd.get("quantity", 0) if pd.get("quantity") else 0) if is_purchase else 0.0
        total_amount_auto = float(pd.get("total_amount", 0) if pd.get("total_amount") else 0) if is_purchase else float(run_result.get("total_amount", 0) or 0)

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
                    "ingredient_id": ingredient_id,
                    "quantity": quantity_auto,
                    "unit_price": float(bundle["current_price"]),
                    "total_price": total_amount_auto,
                    "supplier_id": int(bundle["supplier_id"]),
                },
            )

        # 低风险自动放行（无需人工审批）直接调用强绑定物理入库，
        # 库存原子累加 + 订单置 COMPLETED + 写 completed_at + commit。
        if quantity_auto > 0:
            _ = await execute_inbound_stock(db, order_id)
            final_status = "COMPLETED"
            await db.execute(
                text(
                    """
                    UPDATE purchase_orders
                    SET total_amount = :total_amount, demand_reasoning = :demand_reasoning
                    WHERE id = :order_id
                    """
                ),
                {
                    "total_amount": total_amount_auto,
                    "demand_reasoning": run_result.get("demand_reasoning", ""),
                    "order_id": order_id,
                },
            )
            await db.commit()
        else:
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

    for name in low_stock_ingredients:
        bundle = await _load_ingredient_bundle(db, name)
        if bundle is None:
            continue

        lock = await acquire_ingredient_lock(bundle["ingredient_id"])
        if lock is None:
            # 其它 worker 正在处理该 ingredient —— 跳过，不让整个扫描阻塞
            continue

        try:
            await _process_one(bundle)
        finally:
            await release_ingredient_lock(lock)

    return triggered
