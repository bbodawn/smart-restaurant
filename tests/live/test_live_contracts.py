"""LIVE E2E：入库幂等 / 重复审批 / 食材锁粒度 / 同食材并发 / 异食材并行。

真实 MySQL（restaurant_it）+ 真实 Redis。未开启 TEST_LIVE 时整组 skip → NOT RUN。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.api.purchase import ApproveRequest, approve_purchase_order
from app.core.lock import acquire_ingredient_lock, release_ingredient_lock
from app.services.auto_procurement import scan_and_trigger_procurement
from app.services.inventory import execute_inbound_stock

pytestmark = pytest.mark.live


async def _insert_order(db, *, status="RUNNING", thread=None, order_no=None, total_amount=0.0):
    order_no = order_no or "PO-" + uuid.uuid4().hex[:16].upper()
    thread = thread or uuid.uuid4().hex
    await db.execute(
        text("""INSERT INTO purchase_orders (order_no, thread_id, status, source, total_amount)
                VALUES (:o,:t,:s,'AUTO',:a)"""),
        {"o": order_no, "t": thread, "s": status, "a": total_amount},
    )
    order_id = int((await db.execute(
        text("SELECT id FROM purchase_orders WHERE order_no = :o"), {"o": order_no})).scalar_one())
    await db.commit()
    return {"order_id": order_id, "order_no": order_no, "thread_id": thread}


async def _insert_item(db, order_id, ing, quantity, price, supplier_id):
    await db.execute(
        text("""INSERT INTO purchase_order_items
                (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                VALUES (:o,:i,:q,:u,:t,:s)"""),
        {"o": order_id, "i": ing, "q": quantity, "u": price,
         "t": round(quantity * price, 2), "s": supplier_id},
    )
    await db.commit()


def _graph_snapshot(order, ing) -> dict:
    return {
        "order_id": order["order_id"],
        "order_no": order["order_no"],
        "thread_id": order["thread_id"],
        "ingredient_id": ing["ingredient_id"],
        "ingredient": ing["name"],
        "unit": "kg",
        "current_stock": ing["current_stock"],
        "safety_stock": ing["safety_stock"],
        "daily_sales": ing["daily_sales"],
        "predicted_demand": 0.0,
        "quantity": 0.0,
        "demand_reasoning": "",
        "supplier_id": ing["supplier_id"],
        "supplier_name": ing["name"] + "供应商",
        "supplier_price": ing["price"],
        "historical_price": ing["historical_price"],
        "price_deviation": 0.0,
        "total_amount": 0.0,
        "risk_analysis_report": None,
        "risk_reason": None,
        "approved": None,
        "status": "RUNNING",
    }


async def _stock_of(db, ingredient_id):
    val = await db.execute(text("SELECT current_stock FROM inventory WHERE ingredient_id = :i"),
                           {"i": ingredient_id})
    return float(val.scalar_one())


async def _completed_orders(db, ingredient_id):
    res = await db.execute(
        text("""SELECT po.id FROM purchase_orders po
                JOIN purchase_order_items poi ON poi.order_id = po.id
                WHERE poi.ingredient_id = :i AND po.status = 'COMPLETED'"""),
        {"i": ingredient_id},
    )
    return res.fetchall()


# ---------- 1. 入库幂等 ----------

async def test_inbound_is_idempotent(db, seed_ingredient):
    ing = await seed_ingredient(db, stock=100, daily=5, safety=30, price=5, hist=5)
    order = await _insert_order(db)
    await _insert_item(db, order["order_id"], ing["ingredient_id"], quantity=10, price=5, supplier_id=ing["supplier_id"])

    first = await execute_inbound_stock(db, order["order_id"])
    assert first["status"] == "COMPLETED"
    assert first["restocked_quantity"] == 10.0
    assert await _stock_of(db, ing["ingredient_id"]) == 110.0

    second = await execute_inbound_stock(db, order["order_id"])
    assert second["already_completed"] is True
    assert second["restocked_quantity"] == 0.0
    assert await _stock_of(db, ing["ingredient_id"]) == 110.0  # 不重复入库


# ---------- 2. 重复 approve 不重复入库 ----------

async def test_duplicate_approve_restocks_once(db, seed_ingredient, graph, no_llm):
    # 低库存 + 价格偏离 → REVIEW，挂起等人工
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    order = await _insert_order(db)
    config = {"configurable": {"thread_id": order["thread_id"]}}
    result = await graph.ainvoke(_graph_snapshot(order, ing), config=config)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    await _insert_item(db, order["order_id"], ing["ingredient_id"],
                       quantity=float(payload["quantity"]), price=float(payload["supplier_price"]),
                       supplier_id=ing["supplier_id"])
    await db.execute(
        text("UPDATE purchase_orders SET status='SUSPENDED', risk_reason=:r WHERE id=:o"),
        {"r": payload.get("risk_reason"), "o": order["order_id"]},
    )
    await db.commit()

    request = type("Req", (), {"app": type("App", (), {"state": type("St", (), {"graph": graph})()})()})()
    first = await approve_purchase_order(order["order_id"], ApproveRequest(approved=True), request, db)
    assert first["status"] == "COMPLETED"
    assert first["restocked_quantity"] == pytest.approx(140.0)
    stock_after_first = await _stock_of(db, ing["ingredient_id"])
    assert stock_after_first == pytest.approx(150.0)

    second = await approve_purchase_order(order["order_id"], ApproveRequest(approved=True), request, db)
    assert second["status"] == "COMPLETED"
    assert "message" in second  # 非 SUSPENDED，提前返回
    assert await _stock_of(db, ing["ingredient_id"]) == pytest.approx(stock_after_first)  # 未再入库


# ---------- 3. Redis ingredient 锁：同食材互斥、异食材并行 ----------

async def test_ingredient_lock_granularity():
    # 两个不同食材可同时持有
    l1 = await acquire_ingredient_lock(90_001)
    l2 = await acquire_ingredient_lock(90_002)
    assert l1 is not None and l2 is not None
    # 同一食材第二次获取（非阻塞）失败
    dup = await acquire_ingredient_lock(90_001)
    assert dup is None
    if l1:
        await release_ingredient_lock(l1)
    if l2:
        await release_ingredient_lock(l2)


# ---------- 4/5. 自动采购并发：同食材只建一单，异食材互不阻塞 ----------

async def _scan(session_factory, graph):
    async with session_factory() as s:
        return await scan_and_trigger_procurement(s, graph)


async def test_same_ingredient_concurrent_creates_single_order(_session_factory, graph, seed_ingredient):
    # 无风险自动采购食材：cur>=safety 但 < 3日需求
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)

    await asyncio.gather(_scan(_session_factory, graph), _scan(_session_factory, graph))

    async with _session_factory() as s:
        done = await _completed_orders(s, ing["ingredient_id"])
        stock = await _stock_of(s, ing["ingredient_id"])
    assert len(done) == 1                 # 只建 1 单
    assert stock == pytest.approx(150.0)  # 库存只 +N 一次（60 + 90）


async def test_different_ingredients_parallel_both_complete(_session_factory, graph, seed_ingredient):
    async with _session_factory() as s:
        ing_a = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)
        ing_b = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)

    await asyncio.gather(_scan(_session_factory, graph), _scan(_session_factory, graph))

    async with _session_factory() as s:
        a_done = await _completed_orders(s, ing_a["ingredient_id"])
        b_done = await _completed_orders(s, ing_b["ingredient_id"])
        sa = await _stock_of(s, ing_a["ingredient_id"])
        sb = await _stock_of(s, ing_b["ingredient_id"])
    # 两个食材都各自完成一次采购（不被全局锁互相阻塞）
    assert len(a_done) == 1 and len(b_done) == 1
    assert sa == pytest.approx(150.0) and sb == pytest.approx(150.0)
