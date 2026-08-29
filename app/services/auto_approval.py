import os
from typing import Any, Dict, List

from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date
from app.services.inventory import execute_inbound_stock

# 挂起单超过该虚拟天数（业务天数）仍未人工审批，则自动放行补库存
AUTO_APPROVE_AFTER_DAYS = int(os.getenv("AUTO_APPROVE_AFTER_DAYS", "3"))


async def _complete_and_restock(
    db: AsyncSession, graph: Any, order_id: int, thread_id: str
) -> Dict[str, Any]:
    """恢复状态机，然后通过强绑定物理入库补齐库存并置 COMPLETED。"""
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(Command(resume={"approved": True}), config=config)

    inbound = await execute_inbound_stock(db, order_id)
    return {
        "order_id": order_id,
        "restocked_quantity": inbound.get("restocked_quantity", 0),
        "status": "COMPLETED",
    }


async def auto_approve_suspended_orders(
    db: AsyncSession, graph: Any, threshold_days: int = AUTO_APPROVE_AFTER_DAYS
) -> List[Dict[str, Any]]:
    """自动放行超过阈值天数仍待人工审批的挂起单，并回补库存。"""
    current = get_current_date()
    # 取出所有挂起单，用虚拟日期之差判断是否超时
    result = await db.execute(
        text(
            """
            SELECT id, thread_id, suspended_virtual_date
            FROM purchase_orders
            WHERE status = 'SUSPENDED'
              AND suspended_virtual_date IS NOT NULL
            ORDER BY id
            """
        )
    )

    approved: List[Dict[str, Any]] = []
    rows = result.fetchall()
    for row in rows:
        order_id, thread_id, suspended = row
        if suspended is None:
            continue
        elapsed = (current - suspended).days
        if elapsed >= threshold_days:
            approved.append(await _complete_and_restock(db, graph, order_id, thread_id))

    return approved
