"""Phase 10-A 统一库存流水验证（LIVE DB，restaurant_it）。

覆盖（Section 2 三个行为）：
1. consume_inventory 成功扣减：库存逐食材减少、每食材一条负流水、balance_after==库存。
2. consume_inventory 任一食材不足 → 整单原子拒绝（库存/流水零写入），抛 InsufficientStock。
3. execute_inbound_stock 采购入库同事务写 stock_movements(INBOUND, PURCHASE_ORDER)。

未开启 TEST_LIVE 时整组 skip → NOT RUN。
"""
import uuid

import pytest
from sqlalchemy import text

from app.services.inventory import InsufficientStock, consume_inventory, execute_inbound_stock

pytestmark = pytest.mark.live


async def _stock(s, ingredient_id):
    return float((await s.execute(
        text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ingredient_id})).scalar_one())


async def _movements(s, ingredient_id=None, movement_type=None, reference_id=None):
    sql = "SELECT ingredient_id, change_qty, balance_after, movement_type, reference_type, reference_id, virtual_date FROM stock_movements"
    cond, params = [], {}
    if ingredient_id is not None:
        cond.append("ingredient_id = :i"); params["i"] = ingredient_id
    if movement_type is not None:
        cond.append("movement_type = :m"); params["m"] = movement_type
    if reference_id is not None:
        cond.append("reference_id = :rid"); params["rid"] = reference_id
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY id"
    return [dict(r) for r in (await s.execute(text(sql), params)).mappings()]


async def _movement_count(s, **kw):
    return len(await _movements(s, **kw))


async def _order_with_item(s, ing, qty=10.0, unit_price=5.0, source="AUTO"):
    order_no = "PO-" + uuid.uuid4().hex[:16].upper()
    thread = uuid.uuid4().hex
    await s.execute(
        text("""INSERT INTO purchase_orders (order_no, thread_id, status, source)
                VALUES (:o, :t, 'RUNNING', :src)"""),
        {"o": order_no, "t": thread, "src": source},
    )
    oid = int((await s.execute(text("SELECT id FROM purchase_orders WHERE order_no=:o"), {"o": order_no})).scalar_one())
    await s.execute(
        text("""INSERT INTO purchase_order_items
                (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                VALUES (:o, :i, :q, :u, :t, :sup)"""),
        {"o": oid, "i": ing["ingredient_id"], "q": qty, "u": unit_price,
         "t": round(qty * unit_price, 2), "sup": ing["supplier_id"]},
    )
    await s.commit()
    return oid


# ---------- 1. consume_inventory 成功扣减 ----------

async def test_consume_success_writes_movements_and_deducts(db, seed_ingredient):
    ing1 = await seed_ingredient(db, stock=50, daily=5, safety=10, price=5, hist=5)
    ing2 = await seed_ingredient(db, stock=30, daily=3, safety=10, price=8, hist=8)
    reference_id = 999001  # 测试期模拟 sales_order.id；真实值由 Section 3 销售订单产生

    rows = await consume_inventory(
        db,
        [
            {"ingredient_id": ing1["ingredient_id"], "qty": 10.0},
            {"ingredient_id": ing2["ingredient_id"], "qty": 4.0},
        ],
        movement_type="ORDER_SALE",
        reference_type="SALES_ORDER",
        reference_id=reference_id,
        virtual_date="2026-09-08",
        note="测试点单扣减",
    )

    # 返回值含每食材一条（change_qty 为负、balance_after 与库存一致）
    assert {r["ingredient_id"] for r in rows} == {ing1["ingredient_id"], ing2["ingredient_id"]}
    for r in rows:
        assert r["change_qty"] < 0
    assert await _stock(db, ing1["ingredient_id"]) == 40.0
    assert await _stock(db, ing2["ingredient_id"]) == 26.0
    # 流水：逐食材各一条 ORDER_SALE，balance_after 恒等于扣减后库存
    assert await _movement_count(db, reference_id=reference_id) == 2
    by_ing = {r["ingredient_id"]: r for r in await _movements(db, reference_id=reference_id)}
    assert set(by_ing) == {ing1["ingredient_id"], ing2["ingredient_id"]}
    for ing, expected in [(ing1, 40.0), (ing2, 26.0)]:
        mv = by_ing[ing["ingredient_id"]]
        assert mv["movement_type"] == "ORDER_SALE"
        assert mv["reference_type"] == "SALES_ORDER"
        assert float(mv["change_qty"]) == -(10.0 if ing["ingredient_id"] == ing1["ingredient_id"] else 4.0)
        assert float(mv["balance_after"]) == expected
        assert await _stock(db, ing["ingredient_id"]) == expected


# ---------- 2. 任一不足 → 整单原子拒绝 ----------

async def test_consume_insufficient_rejects_whole_order_zero_write(_session_factory, seed_ingredient):
    # 用独立 session 并在测试末尾清理 seed 食材：
    # 本用例故意 seed 低库存食材(3kg)验证缺料，若不清理会残留在共享库 restaurant_it，
    # 被后续 suite 的 scan_and_trigger_procurement 扫到 → 跨用例干扰（见 phase5b 依赖单候选）。
    async with _session_factory() as seed_s:
        ing1 = await seed_ingredient(seed_s, stock=50, daily=5, safety=10, price=5, hist=5)
        ing2 = await seed_ingredient(seed_s, stock=3, daily=3, safety=10, price=8, hist=8)  # 只够 3，要扣 10
    reference_id = 999002

    async with _session_factory() as s:
        try:
            before1 = await _stock(s, ing1["ingredient_id"])
            before2 = await _stock(s, ing2["ingredient_id"])

            with pytest.raises(InsufficientStock) as exc:
                await consume_inventory(
                    s,
                    [
                        {"ingredient_id": ing1["ingredient_id"], "qty": 10.0},
                        {"ingredient_id": ing2["ingredient_id"], "qty": 10.0},  # 不足 → 整单拒绝
                    ],
                    movement_type="ORDER_SALE",
                    reference_type="SALES_ORDER",
                    reference_id=reference_id,
                    virtual_date="2026-09-08",
                )
            missing = exc.value.missing
            assert len(missing) == 1 and missing[0]["ingredient_id"] == ing2["ingredient_id"]
            assert missing[0]["required"] == 10.0 and missing[0]["current"] == 3.0

            # 零写入：充足食材也未被部分扣减，流水无任何该 reference 记录
            assert await _stock(s, ing1["ingredient_id"]) == before1
            assert await _stock(s, ing2["ingredient_id"]) == before2
            assert await _movement_count(s, reference_id=reference_id) == 0
        finally:
            await _cleanup_ingredient(s, ing1["ingredient_id"])
            await _cleanup_ingredient(s, ing2["ingredient_id"])


async def _cleanup_ingredient(s, ingredient_id):
    """删除 seed 食材（含依赖行），避免低库存残留污染后续共享 suite。"""
    await s.execute(text("DELETE FROM stock_movements WHERE ingredient_id = :i"), {"i": ingredient_id})
    await s.execute(text("DELETE FROM suppliers WHERE ingredient_id = :i"), {"i": ingredient_id})
    await s.execute(text("DELETE FROM inventory WHERE ingredient_id = :i"), {"i": ingredient_id})
    await s.execute(text("DELETE FROM ingredients WHERE id = :i"), {"i": ingredient_id})
    await s.commit()


# ---------- 2b. 同一食材多行（多菜共用料）→ 聚合为一条流水、扣减一次 ----------

async def test_consume_same_ingredient_multi_rows_aggregates(_session_factory, seed_ingredient):
    async with _session_factory() as seed_s:
        ing = await seed_ingredient(seed_s, stock=50, daily=5, safety=10, price=5, hist=5)
    reference_id = 999003

    async with _session_factory() as s:
        try:
            rows = await consume_inventory(
                s,
                [
                    {"ingredient_id": ing["ingredient_id"], "qty": 10.0},
                    {"ingredient_id": ing["ingredient_id"], "qty": 4.0},  # 同一食材第二行
                ],
                movement_type="ORDER_SALE",
                reference_type="SALES_ORDER",
                reference_id=reference_id,
                virtual_date="2026-09-08",
            )
            # 聚合为一条结果（不重复扣）
            assert len(rows) == 1 and rows[0]["ingredient_id"] == ing["ingredient_id"]
            assert float(rows[0]["change_qty"]) == -14.0
            assert float(rows[0]["balance_after"]) == 36.0
            # 库存只减 14；流水仅一条
            assert await _stock(s, ing["ingredient_id"]) == 36.0
            assert await _movement_count(s, reference_id=reference_id) == 1
        finally:
            await _cleanup_ingredient(s, ing["ingredient_id"])


# ---------- 3. 采购入库同事务写 INBOUND 流水 ----------

async def test_inbound_writes_stock_movement(db, seed_ingredient):
    ing = await seed_ingredient(db, stock=50, daily=5, safety=10, price=5, hist=5)
    oid = await _order_with_item(db, ing, qty=10.0, unit_price=5.0)

    r1 = await execute_inbound_stock(db, oid)
    assert r1["status"] == "COMPLETED"

    mv = await _movements(db, ingredient_id=ing["ingredient_id"], movement_type="INBOUND")
    assert len(mv) == 1
    assert mv[0]["reference_type"] == "PURCHASE_ORDER"
    assert int(mv[0]["reference_id"]) == oid
    assert float(mv[0]["change_qty"]) == 10.0
    assert float(mv[0]["balance_after"]) == 60.0

    # 幂等：二次入库不再新增流水
    await execute_inbound_stock(db, oid)
    assert len(await _movements(db, ingredient_id=ing["ingredient_id"], movement_type="INBOUND")) == 1
