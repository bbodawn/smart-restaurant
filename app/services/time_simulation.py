from typing import Dict, Any, List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.clock import advance_virtual_days, get_current_date
from app.services.auto_approval import auto_approve_suspended_orders
from app.services.auto_procurement import scan_and_trigger_procurement

async def simulate_days_passing(
    db: AsyncSession, days: int = 1, graph: Any = None
) -> Dict[str, Any]:
    # 0. 记录推进前的日期（本次扣减对应的营业日，用于出库日志归属）
    previous_date = get_current_date()

    # 1. 推进虚拟时间
    new_date = advance_virtual_days(days)

    # 2. 从数据库读取所有食材与其库存信息（联表 ingredients + inventory）
    #    每日消耗量取 inventory.daily_sales；若无效则默认为 50.0kg
    query = text("""
        SELECT i.id, i.name, inv.current_stock, inv.safety_stock, inv.daily_sales
        FROM ingredients i
        JOIN inventory inv ON inv.ingredient_id = i.id
    """)
    result = await db.execute(query)
    ingredients = result.fetchall()

    low_stock_alerts: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []

    for ing in ingredients:
        ing_id, name, current_stock, safety_stock, daily_sales = ing
        consumption_rate = float(daily_sales) if daily_sales and daily_sales > 0 else 50.0

        # 扣减库存，最小不能小于 0
        consumed = consumption_rate * days
        new_stock = max(0.0, float(current_stock) - consumed)

        # 更新数据库中的当前库存
        update_stmt = text("""
            UPDATE inventory
            SET current_stock = :new_stock
            WHERE ingredient_id = :ing_id
        """)
        await db.execute(update_stmt, {"new_stock": new_stock, "ing_id": ing_id})

        # 检查是否触及或低于安全库存线
        is_low = new_stock <= float(safety_stock)
        item_info = {
            "ingredient": name,
            "previous_stock": float(current_stock),
            "consumed": consumed,
            "new_stock": new_stock,
            "safety_stock": float(safety_stock),
            "is_low_stock": is_low
        }
        updated_items.append(item_info)

        if is_low:
            low_stock_alerts.append(item_info)

    await db.commit()

    # 3. 自动放行超过阈值天数仍待人工审批的挂起单（HITL 兜底）
    auto_approved: List[Dict[str, Any]] = []
    if graph is not None:
        auto_approved = await auto_approve_suspended_orders(db, graph)

    # 4. 巡检低于安全库存线的食材，自动触发采购
    auto_procurement: List[Dict[str, Any]] = []
    if graph is not None:
        auto_procurement = await scan_and_trigger_procurement(db, graph)

    return {
        "current_virtual_date": new_date.isoformat(),
        "previous_virtual_date": previous_date.isoformat(),
        "days_advanced": days,
        "updated_ingredients": updated_items,
        "low_stock_alerts": low_stock_alerts,
        "auto_procurement_triggered": auto_procurement,
        "auto_approved_suspended": auto_approved,
    }
