"""Phase 10-C 快进 SIMULATION_SALE 真实销售链路 LIVE E2E（restaurant_it）。

覆盖（Section 4）：
1. 快进消耗不再直接 UPDATE inventory：simulate_days_passing 生成 order_type=SIMULATION_SALE
   的真实销售订单 → dish_bom 展开 → consume_inventory → stock_movements(movement_type=SIMULATION_SALE)。
2. 一致性：库存扣减量 == SIMULATION_SALE 流水累计 == 初始-最终库存差。
3. daily_sales 不被 simulation 修改（仍保留，仅预测基线）。
4. 快进多天 → 每营业日各生成模拟单、virtual_date 逐日正确、逐日触发采购扫描。
5. SIMULATION_SALE 订单不污染真实销量分布（recent 分布只统计 ORDER_SALE —— 由 SQL 过滤保证）。

seed 隔离纪律同 phase10b：uuid 食材+自引用菜，末尾清理全部依赖行。

未开启 TEST_LIVE 时整组 skip → NOT RUN。
"""
import uuid

import pytest
from sqlalchemy import text

from app.services.time_simulation import simulate_days_passing

pytestmark = pytest.mark.live


async def _seed_dish_selfref(_session_factory, *, stock, price=20.0, qty=0.5, sim_qty=2):
    async with _session_factory() as s:
        tag = uuid.uuid4().hex[:8]
        ing_name = f"SIM-ING-{tag}"
        dish_name = f"SIM-DISH-{tag}"
        await s.execute(text("INSERT INTO ingredients (name, unit, category) VALUES (:n,'kg','测试')"), {"n": ing_name})
        ing_id = int((await s.execute(text("SELECT id FROM ingredients WHERE name=:n"), {"n": ing_name})).scalar_one())
        await s.execute(
            text("INSERT INTO inventory (ingredient_id, current_stock, daily_sales, safety_stock) VALUES (:i,:c,:d,:f)"),
            {"i": ing_id, "c": stock, "d": 5.0, "f": 10.0},
        )
        await s.execute(
            text("INSERT INTO suppliers (name, ingredient_id, current_price, historical_avg_price, rating) VALUES (:n,:i,:p,:h,4.5)"),
            {"n": ing_name + "供应商", "i": ing_id, "p": price, "h": price},
        )
        await s.execute(text("INSERT INTO dishes (name, category, price, simulation_daily_qty) VALUES (:n,'测试菜',:p,:q)"),
                        {"n": dish_name, "p": price, "q": sim_qty})
        dish_id = int((await s.execute(text("SELECT id FROM dishes WHERE name=:n"), {"n": dish_name})).scalar_one())
        await s.execute(
            text("INSERT INTO dish_bom (dish_id, ingredient_id, qty_per_serving) VALUES (:d,:i,:q)"),
            {"d": dish_id, "i": ing_id, "q": qty},
        )
        await s.commit()
        return {"dish_id": dish_id, "dish_name": dish_name, "ingredient_id": ing_id,
                "ing_name": ing_name, "sim_qty": sim_qty}


async def _cleanup(_session_factory, snap):
    async with _session_factory() as s:
        oids = [r[0] for r in (await s.execute(
            text("SELECT DISTINCT so.id FROM sales_orders so "
                 "JOIN sales_order_items soi ON soi.order_id=so.id WHERE soi.dish_id=:d"),
            {"d": snap["dish_id"]})).fetchall()]
        if oids:
            marks = ",".join(f":o{n}" for n in range(len(oids)))
            params = {f"o{n}": o for n, o in enumerate(oids)}
            await s.execute(text(f"DELETE FROM sales_order_items WHERE order_id IN ({marks})"), params)
            await s.execute(text(f"DELETE FROM sales_orders WHERE id IN ({marks})"), params)
        await s.execute(text("DELETE FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM dish_bom WHERE dish_id=:d"), {"d": snap["dish_id"]})
        await s.execute(text("DELETE FROM dishes WHERE id=:d"), {"d": snap["dish_id"]})
        await s.execute(text("DELETE FROM suppliers WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM inventory WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM ingredients WHERE id=:i"), {"i": snap["ingredient_id"]})
        await s.commit()


async def _stock_of(s, ingredient_id):
    return float((await s.execute(
        text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ingredient_id})).scalar_one())


async def _daily_sales_of(s, ingredient_id):
    return float((await s.execute(
        text("SELECT daily_sales FROM inventory WHERE ingredient_id=:i"), {"i": ingredient_id})).scalar_one())


async def _sim_movements_sum(s, snap):
    val = (await s.execute(text(
        "SELECT COALESCE(SUM(change_qty),0) FROM stock_movements "
        "WHERE ingredient_id=:i AND movement_type='SIMULATION_SALE'"), {"i": snap["ingredient_id"]})).scalar_one()
    return float(val)


async def _sim_orders(s, snap):
    rows = (await s.execute(text(
        "SELECT so.id, so.virtual_date, so.order_type FROM sales_orders so "
        "JOIN sales_order_items soi ON soi.order_id=so.id WHERE soi.dish_id=:d AND so.order_type='SIMULATION_SALE'"),
        {"d": snap["dish_id"]})).mappings().all()
    return rows


# ---------- 1/2/3. 快进 1 天：真实销售链路 + 一致性 + daily_sales 不变 ----------

async def test_advance_one_day_uses_simulation_sale_not_direct_update(_session_factory, seed_ingredient):
    # graph=None：跳过 auto_approve/scan，隔离验证「模拟扣减」本身
    snap = await _seed_dish_selfref(_session_factory, stock=100.0, qty=0.5, sim_qty=2)
    try:
        before = None
        async with _session_factory() as s:
            before = await _stock_of(s, snap["ingredient_id"])
            daily_before = await _daily_sales_of(s, snap["ingredient_id"])
            res = await simulate_days_passing(s, days=1, graph=None)
            await s.commit()

            after = await _stock_of(s, snap["ingredient_id"])
            consumed = before - after

            # 生成 SIMULATION_SALE 单 + 流水
            orders = await _sim_orders(s, snap)
            assert len(orders) == 1 and orders[0]["order_type"] == "SIMULATION_SALE"
            mv_sum = await _sim_movements_sum(s, snap)
            # 一致性：库存差 == 流水负和 == BOM×份数
            assert consumed == pytest.approx(1.0)          # 0.5 × 2 份
            assert mv_sum == pytest.approx(-consumed)
            assert mv_sum == pytest.approx(-1.0)
            # daily_sales 未被修改
            assert await _daily_sales_of(s, snap["ingredient_id"]) == pytest.approx(daily_before)
            assert res["days_advanced"] == 1
            assert res["previous_virtual_date"] and res["current_virtual_date"]
    finally:
        await _cleanup(_session_factory, snap)


# ---------- 4. 快进多天：逐日生成 + 逐日巡检 ----------

async def test_advance_multi_days_daily_orders_and_scan(_session_factory, graph, seed_ingredient):
    snap = await _seed_dish_selfref(_session_factory, stock=200.0, qty=0.5, sim_qty=2)
    try:
        async with _session_factory() as s:
            before = await _stock_of(s, snap["ingredient_id"])
            daily_before = await _daily_sales_of(s, snap["ingredient_id"])
            res = await simulate_days_passing(s, days=3, graph=graph)
            await s.commit()

            orders = await _sim_orders(s, snap)
            # 3 天 → 3 张 SIMULATION_SALE 单，virtual_date 逐日（同日去重后 ==3）
            dates = sorted({str(o["virtual_date"]) for o in orders})
            assert len(orders) == 3 and len(dates) == 3
            consumed = before - await _stock_of(s, snap["ingredient_id"])
            assert consumed == pytest.approx(0.5 * 2 * 3)  # 0.5×2份×3天
            # 逐日巡检触发（res 里应有 auto_procurement_triggered 列表结构；3天至少返回该键）
            assert isinstance(res["auto_procurement_triggered"], list)
            assert isinstance(res["auto_approved_suspended"], list)
            # daily_sales 不变
            assert await _daily_sales_of(s, snap["ingredient_id"]) == pytest.approx(daily_before)
    finally:
        await _cleanup(_session_factory, snap)


# ---------- 5. SIMULATION_SALE 不污染真实销量分布（历史分布只统计 ORDER_SALE） ----------

async def test_simulation_does_not_pollute_real_sales_distribution(_session_factory, seed_ingredient):
    from app.services.time_simulation import _recent_order_sale_rows
    from app.services.sales import recent_order_sale_distribution

    # 造两套独立食材+菜：A 产生一笔真实点单；B 只产生一笔模拟单
    dish_a = await _seed_dish_selfref(_session_factory, stock=200.0, qty=0.5, sim_qty=2)
    dish_b = await _seed_dish_selfref(_session_factory, stock=200.0, qty=0.5, sim_qty=2)
    try:
        async with _session_factory() as s:
            from app.core.clock import get_current_date
            from app.services.sales import create_sales_order

            # 真实 ORDER_SALE：dish_a ×2
            so_real = await create_sales_order(
                s, line_items=[{"dish_id": dish_a["dish_id"], "qty": 2}],
                created_by="test", order_type="ORDER_SALE", virtual_date=get_current_date(),
            )
            # 模拟 SIMULATION_SALE：dish_b ×3（绝不该计入分布）
            await create_sales_order(
                s, line_items=[{"dish_id": dish_b["dish_id"], "qty": 3}],
                created_by="simulation", order_type="SIMULATION_SALE", virtual_date=get_current_date(),
            )
            await s.commit()

            # 分布只统计 ORDER_SALE → dish_a 独占、dish_b 不出现
            rows = await _recent_order_sale_rows(s)
            dist = recent_order_sale_distribution(rows)
            assert set(dist) == {dish_a["dish_id"]}
            assert dist[dish_a["dish_id"]] == pytest.approx(1.0)
            assert dish_b["dish_id"] not in dist
            assert so_real["order_type"] == "ORDER_SALE"
    finally:
        await _cleanup(_session_factory, dish_a)
        await _cleanup(_session_factory, dish_b)


# ---------- 库存不足：模拟跳过该菜、不进负库存、整体成功 ----------

async def test_advance_skips_insufficient_dish_no_negative(_session_factory, seed_ingredient):
    snap = await _seed_dish_selfref(_session_factory, stock=0.5, qty=0.5, sim_qty=2)  # 只够 1 份，需 2
    try:
        async with _session_factory() as s:
            before = await _stock_of(s, snap["ingredient_id"])
            res = await simulate_days_passing(s, days=1, graph=None)
            await s.commit()

            after = await _stock_of(s, snap["ingredient_id"])
            # 该菜目标 2 份需 1.0 > 0.5 → 跳过：库存不变、无 SIMULATION_SALE 流水、不进负库存
            assert after == pytest.approx(before)
            assert await _sim_movements_sum(s, snap) == 0.0
            assert after >= 0
            assert res["days_advanced"] == 1
    finally:
        await _cleanup(_session_factory, snap)
