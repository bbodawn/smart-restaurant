from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import advance_virtual_days, get_current_date
from app.services.auto_approval import auto_approve_suspended_orders
from app.services.auto_procurement import scan_and_trigger_procurement
from app.services.inventory import InsufficientStock
from app.services.sales import (
    create_sales_order,
    recent_order_sale_distribution,
    simulate_sales_strategy,
)

# 真实历史分布窗口：只统计近 N 天 ORDER_SALE（SIMULATION_SALE 不参与模拟销量分布）
_HISTORY_WINDOW_DAYS = 7


async def _load_simulatable_dishes(db: AsyncSession) -> List[Dict[str, Any]]:
    """可售且有 BOM 的菜（每日模拟候选）。无 BOM 的菜不生成模拟单（避免空扣）。"""
    rows = (
        await db.execute(
            text(
                """
                SELECT d.id AS dish_id, d.simulation_daily_qty
                FROM dishes d
                WHERE d.available = 1
                  AND EXISTS (SELECT 1 FROM dish_bom b WHERE b.dish_id = d.id)
                ORDER BY d.id
                """
            )
        )
    ).mappings().all()
    return [{"dish_id": r["dish_id"], "simulation_daily_qty": int(r["simulation_daily_qty"] or 0)} for r in rows]


async def _recent_order_sale_rows(db: AsyncSession) -> List[Dict[str, Any]]:
    """近窗口真实点单明细（ORDER_SALE only，SIMULATION_SALE 绝不计入历史分布）。"""
    rows = (
        await db.execute(
            text(
                """
                SELECT soi.dish_id, soi.qty
                FROM sales_order_items soi
                JOIN sales_orders so ON so.id = soi.order_id
                WHERE so.order_type = 'ORDER_SALE'
                  AND so.virtual_date >= DATE_SUB(CURDATE(), INTERVAL :w DAY)
                """
            ),
            {"w": _HISTORY_WINDOW_DAYS},
        )
    ).mappings().all()
    return [{"dish_id": r["dish_id"], "qty": int(r["qty"])} for r in rows]


async def _stock_snapshot(db: AsyncSession) -> Dict[int, float]:
    """全食材库存快照（只读，绝不作为扣减手段）。"""
    rows = (await db.execute(text("SELECT ingredient_id, current_stock FROM inventory"))).mappings().all()
    return {r["ingredient_id"]: float(r["current_stock"]) for r in rows}


async def _ingredient_name_map(db: AsyncSession) -> Dict[int, str]:
    rows = (await db.execute(text("SELECT id, name FROM ingredients"))).mappings().all()
    return {r["id"]: r["name"] for r in rows}


async def simulate_days_passing(
    db: AsyncSession, days: int = 1, graph: Any = None
) -> Dict[str, Any]:
    """快进虚拟日期，把「每日营业消耗」建模为 SIMULATION_SALE 真实销售订单。

    替代旧的「按 daily_sales × days 直接 UPDATE inventory」：
    - 逐营业日：推进时钟 1 天 → 按模拟策略生成每菜 SIMULATION_SALE 订单 →
      create_sales_order → consume_inventory → stock_movements → inventory。
    - 逐日巡检：每天模拟完成后 auto_approve + scan（贴近真实逐日运营）。
    - daily_sales 不参与扣减（保留仅作采购预测/看板基线），杜绝双扣。
    - 库存不足的菜跳过并记日志，不影响快进整体；绝不产生负库存。

    返回键与旧版兼容（previous_virtual_date / updated_ingredients / low_stock_alerts /
    auto_procurement_triggered / auto_approved_suspended），并新增
    simulation_orders / simulation_skipped 供展示与排障。
    """
    previous_date = get_current_date()
    updated_items: List[Dict[str, Any]] = []
    low_stock_alerts: List[Dict[str, Any]] = []
    skipped_log: List[Dict[str, Any]] = []
    all_procurement: List[Dict[str, Any]] = []
    all_auto_approved: List[Dict[str, Any]] = []

    # 快进前库存快照（只读基线，用于聚合本次消耗）
    before_snapshot = await _stock_snapshot(db)
    name_map = await _ingredient_name_map(db)
    # 安全线查询（供低库存判定）
    safety_map = {
        r["ingredient_id"]: float(r["safety_stock"])
        for r in (await db.execute(text("SELECT ingredient_id, safety_stock FROM inventory"))).mappings().all()
    }

    dishes = await _load_simulatable_dishes(db)
    daily_qty_map = {d["dish_id"]: d["simulation_daily_qty"] for d in dishes}
    dish_ids = [d["dish_id"] for d in dishes]

    order_summary: List[Dict[str, Any]] = []

    for _offset in range(days):
        current_date = advance_virtual_days(1)  # 逐日推进：巡检看到当日虚拟日期

        # 真实历史分布（只 ORDER_SALE）→ 当日模拟策略
        recent_rows = await _recent_order_sale_rows(db)
        dist = recent_order_sale_distribution(recent_rows)
        strategy = simulate_sales_strategy(dish_ids, daily_qty_map, dist)

        for plan in strategy:
            try:
                so = await create_sales_order(
                    db,
                    line_items=[{"dish_id": plan["dish_id"], "qty": plan["qty"]}],
                    created_by="simulation",
                    order_type="SIMULATION_SALE",
                    virtual_date=current_date,
                )
                order_summary.append(
                    {"order_id": so["order_id"], "order_no": so["order_no"], "virtual_date": str(current_date)}
                )
            except InsufficientStock as exc:
                # 该菜目标份数超当前库存 → 跳过该菜，记日志，不影响当天其它菜 / 快进整体
                skipped_log.append(
                    {
                        "virtual_date": str(current_date),
                        "dish_id": plan["dish_id"],
                        "requested_qty": plan["qty"],
                        "missing": exc.missing,
                    }
                )

        # 逐日巡检：自动放行超时挂起 + 低库存扫描触发采购
        if graph is not None:
            all_auto_approved.extend(await auto_approve_suspended_orders(db, graph))
            all_procurement.extend(await scan_and_trigger_procurement(db, graph))

    # 快进后库存快照 → 聚合本次消耗 + 低库存告警
    after_snapshot = await _stock_snapshot(db)
    for ing_id, prev in before_snapshot.items():
        cur = after_snapshot.get(ing_id, prev)
        consumed = prev - cur
        if consumed <= 0:
            continue
        item_info = {
            "ingredient": name_map.get(ing_id, str(ing_id)),  # 前端按名称显示
            "ingredient_id": ing_id,
            "previous_stock": prev,
            "consumed": round(consumed, 4),
            "new_stock": cur,
            "safety_stock": safety_map.get(ing_id, 0.0),
            "is_low_stock": cur <= safety_map.get(ing_id, 0.0),
        }
        updated_items.append(item_info)
        if cur <= safety_map.get(ing_id, 0.0):
            low_stock_alerts.append(item_info)

    await db.commit()

    return {
        "current_virtual_date": get_current_date().isoformat(),
        "previous_virtual_date": previous_date.isoformat(),
        "days_advanced": days,
        "updated_ingredients": updated_items,
        "low_stock_alerts": low_stock_alerts,
        "auto_procurement_triggered": all_procurement,
        "auto_approved_suspended": all_auto_approved,
        "simulation_orders": order_summary,
        "simulation_skipped": skipped_log,
    }
