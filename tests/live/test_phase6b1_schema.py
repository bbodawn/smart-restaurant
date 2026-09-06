"""Phase 6-B-1 Schema Implementation 验证（LIVE DB，restaurant_it）。

覆盖：source 约束；唯一约束；inbound 成功事务/幂等/其它 IntegrityError 回滚/并发单入库。
未开启 TEST_LIVE 时整组 skip → NOT RUN。
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.services.inventory import execute_inbound_stock

pytestmark = pytest.mark.live


async def _order_with_item(s, ing, qty=10.0, unit_price=5.0, source="AUTO", total=None):
    order_no = "PO-" + uuid.uuid4().hex[:16].upper()
    thread = uuid.uuid4().hex
    await s.execute(
        text("""INSERT INTO purchase_orders (order_no, thread_id, status, source)
                VALUES (:o, :t, 'RUNNING', :src)"""),
        {"o": order_no, "t": thread, "src": source},
    )
    oid = int((await s.execute(text("SELECT id FROM purchase_orders WHERE order_no=:o"), {"o": order_no})).scalar_one())
    total = round(qty * unit_price, 2) if total is None else total
    await s.execute(
        text("""INSERT INTO purchase_order_items
                (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                VALUES (:o, :i, :q, :u, :t, :sup)"""),
        {"o": oid, "i": ing["ingredient_id"], "q": qty, "u": unit_price,
         "t": total, "sup": ing["supplier_id"]},
    )
    await s.commit()
    return oid


async def _stock(s, ingredient_id):
    return float((await s.execute(
        text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ingredient_id})).scalar_one())


async def _inbound_count(s, order_id):
    return int((await s.execute(
        text("SELECT COUNT(*) FROM inbound_records WHERE order_id=:o"), {"o": order_id})).scalar_one())


# ---------- 1. Schema 约束 ----------

async def test_schema_source_column_and_checks(_session_factory):
    async with _session_factory() as s:
        nullable = (await s.execute(text(
            """SELECT IS_NULLABLE FROM information_schema.columns
               WHERE table_schema=DATABASE() AND table_name='purchase_orders' AND column_name='source'"""))).first()
        assert nullable and nullable[0] == "NO"
        for name, table in [("chk_po_source", "purchase_orders"),
                            ("uq_po_item", "purchase_order_items"),
                            ("uq_inbound_record_no", "inbound_records"),
                            ("uq_inbound_order_item", "inbound_records"),
                            ("chk_inbound_qty", "inbound_records"),
                            ("chk_inbound_prices", "inbound_records")]:
            cnt = int((await s.execute(text(
                """SELECT COUNT(*) FROM information_schema.table_constraints
                   WHERE constraint_schema=DATABASE() AND table_name=:t AND constraint_name=:n"""),
                {"t": table, "n": name})).scalar_one())
            assert cnt == 1, f"missing {name} on {table}"
        cols = [r[0] for r in (await s.execute(text(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=DATABASE() AND table_name='inbound_records'"""))).fetchall()]
        for c in ["record_no", "order_id", "order_item_id", "ingredient_id", "inbound_qty",
                  "unit_price", "total_price", "supplier_id", "inbound_virtual_date", "created_at"]:
            assert c in cols, f"missing inbound_records.{c}"


async def test_source_check_rejects_invalid_value(_session_factory):
    async with _session_factory() as s:
        order_no = "PO-" + uuid.uuid4().hex[:16].upper()
        try:
            await s.execute(
                text("INSERT INTO purchase_orders (order_no, thread_id, status, source) VALUES (:o,:t,'RUNNING','X')"),
                {"o": order_no, "t": uuid.uuid4().hex})
            await s.commit()
            raise AssertionError("CHECK 应拒绝非法 source")
        except OperationalError:  # MySQL CHECK(3819) 经 aiomysql 映射为 OperationalError
            await s.rollback()
            cnt = int((await s.execute(text("SELECT COUNT(*) FROM purchase_orders WHERE order_no=:o"), {"o": order_no})).scalar_one())
            assert cnt == 0


# ---------- 2. 成功事务 + 幂等 ----------

async def test_inbound_success_then_idempotent(db, seed_ingredient):
    ing = await seed_ingredient(db, stock=50, daily=5, safety=10, price=5, hist=5)
    oid = await _order_with_item(db, ing, qty=10.0, unit_price=5.0)

    r1 = await execute_inbound_stock(db, oid)
    assert r1["status"] == "COMPLETED" and r1["restocked_quantity"] == 10.0
    assert r1["inbound_record_no"].startswith("INBOUND-")
    assert await _stock(db, ing["ingredient_id"]) == 60.0
    assert await _inbound_count(db, oid) == 1

    r2 = await execute_inbound_stock(db, oid)
    assert r2["already_completed"] is True and r2["restocked_quantity"] == 0.0
    assert await _stock(db, ing["ingredient_id"]) == 60.0
    assert await _inbound_count(db, oid) == 1


# ---------- 3. 其它 IntegrityError → 回滚并继续抛出，不吞 ----------

async def test_inbound_other_integrity_error_rolls_back(_session_factory, seed_ingredient):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=50, daily=5, safety=10, price=5, hist=5)
        # 制造 chk_inbound_prices 冲突（items 无价格约束，可插入负单价）→ 非幂等 DB 错误必须抛出
        oid = await _order_with_item(s, ing, qty=10.0, unit_price=-5.0, total=-50.0)
        with pytest.raises(OperationalError):  # CHECK(3819) → OperationalError，且必须继续抛出
            await execute_inbound_stock(s, oid)
        await s.rollback()
    async with _session_factory() as s:
        assert await _inbound_count(s, oid) == 0
        assert await _stock(s, ing["ingredient_id"]) == 50.0
        st = (await s.execute(text("SELECT status FROM purchase_orders WHERE id=:o"), {"o": oid})).scalar_one()
        assert st != "COMPLETED"  # 无半成功


# ---------- 4. 并发执行：最多一个 inbound / 库存只加一次 ----------

async def test_concurrent_execute_single_inbound(_session_factory, seed_ingredient):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=50, daily=5, safety=10, price=5, hist=5)
        oid = await _order_with_item(s, ing, qty=10.0, unit_price=5.0)

    s1, s2 = _session_factory(), _session_factory()
    try:
        results = await asyncio.gather(
            execute_inbound_stock(s1, oid),
            execute_inbound_stock(s2, oid),
            return_exceptions=True,
        )
    finally:
        await s1.close()
        await s2.close()
    assert all(not isinstance(r, Exception) for r in results), results

    async with _session_factory() as s:
        assert await _inbound_count(s, oid) == 1
        assert await _stock(s, ing["ingredient_id"]) == 60.0
        st = (await s.execute(text("SELECT status FROM purchase_orders WHERE id=:o"), {"o": oid})).scalar_one()
        assert st == "COMPLETED"
