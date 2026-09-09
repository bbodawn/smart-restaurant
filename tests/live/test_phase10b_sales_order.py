"""Phase 10-B 点单销售闭环 LIVE E2E（restaurant_it）。

覆盖（Section 3）：
1. 成功点单：sales_orders/items 落库、库存按 BOM 扣减、stock_movements 写 ORDER_SALE、
   balance_after 与库存一致、订单金额=Σ售价×份数。
2. 缺料 409：任一原料不足 → 整单原子拒绝（订单/明细/流水零落库、库存不变），
   返回 message + missing 缺料清单。
3. 幂等键：同 X-Idempotency-Key 重复 POST → 返回首笔结果，不重复扣库存/不重复建单。
4. 菜单/历史只读：GET menu 含可售菜；GET orders 含新下单。

seed 隔离纪律：每测试用 uuid 造独立「食材+供应商+菜品(自引用 BOM)」，末尾清理全部
依赖行，防止残留被共享库 scan_and_trigger_procurement 扫到（见 phase10a 教训）。

未开启 TEST_LIVE 时整组 skip → NOT RUN。
"""
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from app.core.db import get_db

pytestmark = pytest.mark.live


async def _seed_dish_selfref(_session_factory, *, stock, price, qty):
    """食材 + 供应商 + 菜品 + BOM(该菜只引用自身食材) → 单食材闭环，便于精确断言。"""
    async with _session_factory() as s:
        tag = uuid.uuid4().hex[:8]
        ing_name = f"SO-ING-{tag}"
        dish_name = f"SO-DISH-{tag}"
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
        await s.execute(text("INSERT INTO dishes (name, category, price) VALUES (:n,'测试菜',:p)"),
                        {"n": dish_name, "p": price})
        dish_id = int((await s.execute(text("SELECT id FROM dishes WHERE name=:n"), {"n": dish_name})).scalar_one())
        await s.execute(
            text("INSERT INTO dish_bom (dish_id, ingredient_id, qty_per_serving) VALUES (:d,:i,:q)"),
            {"d": dish_id, "i": ing_id, "q": qty},
        )
        await s.commit()
        return {"dish_id": dish_id, "dish_name": dish_name, "ingredient_id": ing_id, "ing_name": ing_name}


async def _cleanup_dish_order(_session_factory, snap):
    """删除该测试 seed 食材的一切依赖行（FK 安全顺序：先子后父）。"""
    async with _session_factory() as s:
        # 该菜的销售明细 → 其订单（若成孤儿一并删）
        order_ids = [r[0] for r in (await s.execute(
            text("SELECT DISTINCT order_id FROM sales_order_items WHERE dish_id=:d"), {"d": snap["dish_id"]})).fetchall()]
        if order_ids:
            marks = ",".join(f":o{n}" for n in range(len(order_ids)))
            params = {f"o{n}": o for n, o in enumerate(order_ids)}
            await s.execute(text(f"DELETE FROM sales_order_items WHERE order_id IN ({marks})"), params)
            await s.execute(text(f"DELETE FROM sales_orders WHERE id IN ({marks})"), params)
        await s.execute(text("DELETE FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM dish_bom WHERE dish_id=:d"), {"d": snap["dish_id"]})
        await s.execute(text("DELETE FROM dishes WHERE id=:d"), {"d": snap["dish_id"]})
        await s.execute(text("DELETE FROM suppliers WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM inventory WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})
        await s.execute(text("DELETE FROM ingredients WHERE id=:i"), {"i": snap["ingredient_id"]})
        await s.commit()


@pytest_asyncio.fixture
async def client(_session_factory):
    from app.api.sales import router as sales_router

    app = FastAPI()
    app.include_router(sales_router, prefix="/api/v1")

    async def _override():
        async with _session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _stock_of(s, ingredient_id):
    return float((await s.execute(
        text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ingredient_id})).scalar_one())


def _auth_headers_with_idem(auth_headers, role="order_clerk", key=None):
    headers = auth_headers(role)
    headers["X-Idempotency-Key"] = key or ("order-" + uuid.uuid4().hex)
    return headers


async def _create_order(client, headers, dish_id, qty):
    return await client.post(
        "/api/v1/sales/orders",
        json={"items": [{"dish_id": dish_id, "qty": qty}]},
        headers=headers,
    )


# ---------- 1. 成功点单闭环 ----------

async def test_create_sales_order_success_full_chain(_session_factory, client, auth_headers):
    snap = await _seed_dish_selfref(_session_factory, stock=50.0, price=20.0, qty=0.5)
    try:
        r = await _create_order(client, _auth_headers_with_idem(auth_headers), snap["dish_id"], 2)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "COMPLETED"
        assert body["order_no"].startswith("SO-")
        assert body["total_amount"] == pytest.approx(40.0)  # 20*2
        assert len(body["items"]) == 1 and body["items"][0]["qty"] == 2

        async with _session_factory() as s:
            assert await _stock_of(s, snap["ingredient_id"]) == pytest.approx(49.0)  # 50 - 0.5*2
            mv = (await s.execute(text(
                "SELECT change_qty, balance_after, movement_type, reference_type, reference_id "
                "FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})).mappings().all()
            assert len(mv) == 1
            assert mv[0]["movement_type"] == "ORDER_SALE"
            assert mv[0]["reference_type"] == "SALES_ORDER"
            assert int(mv[0]["reference_id"]) == body["order_id"]
            assert float(mv[0]["change_qty"]) == pytest.approx(-1.0)
            assert float(mv[0]["balance_after"]) == pytest.approx(49.0)
            oid = body["order_id"]
            assert (await s.execute(text("SELECT COUNT(*) FROM sales_orders WHERE id=:o"), {"o": oid})).scalar_one() == 1
            assert (await s.execute(text("SELECT COUNT(*) FROM sales_order_items WHERE order_id=:o"), {"o": oid})).scalar_one() == 1
    finally:
        await _cleanup_dish_order(_session_factory, snap)


# ---------- 2. 缺料 → 整单原子拒绝 409 ----------

async def test_create_order_insufficient_409_zero_write(_session_factory, client, auth_headers):
    snap = await _seed_dish_selfref(_session_factory, stock=0.5, price=20.0, qty=0.5)  # 仅够 1 份
    try:
        r = await _create_order(client, _auth_headers_with_idem(auth_headers), snap["dish_id"], 3)  # 需 1.5 > 0.5
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "message" in detail and "missing" in detail
        assert detail["missing"][0]["ingredient_id"] == snap["ingredient_id"]
        assert detail["missing"][0]["required"] == pytest.approx(1.5)
        assert detail["missing"][0]["current"] == pytest.approx(0.5)

        async with _session_factory() as s:
            assert await _stock_of(s, snap["ingredient_id"]) == pytest.approx(0.5)  # 未扣
            # 订单零落库：不存在任何引用该菜的销售明细
            assert (await s.execute(text(
                "SELECT COUNT(*) FROM sales_order_items WHERE dish_id=:d"), {"d": snap["dish_id"]})).scalar_one() == 0
            # 流水零
            assert (await s.execute(text(
                "SELECT COUNT(*) FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})).scalar_one() == 0
    finally:
        await _cleanup_dish_order(_session_factory, snap)


# ---------- 3. 幂等键 ----------

async def test_create_order_idempotency_key(_session_factory, client, auth_headers):
    snap = await _seed_dish_selfref(_session_factory, stock=50.0, price=20.0, qty=0.5)
    try:
        headers = auth_headers("order_clerk")
        headers["X-Idempotency-Key"] = "order-" + uuid.uuid4().hex
        r1 = await _create_order(client, headers, snap["dish_id"], 2)
        assert r1.status_code == 200, r1.text
        r2 = await _create_order(client, headers, snap["dish_id"], 2)
        assert r2.status_code == 200, r2.text
        assert r1.json()["order_no"] == r2.json()["order_no"]  # 重放返回首笔
        async with _session_factory() as s:
            assert await _stock_of(s, snap["ingredient_id"]) == pytest.approx(49.0)  # 只扣一次
            assert (await s.execute(text(
                "SELECT COUNT(*) FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})).scalar_one() == 1
    finally:
        await _cleanup_dish_order(_session_factory, snap)


# ---------- 2b. 不可售菜品 → 拒绝（400，零落库），绝不空扣成交 ----------

async def test_create_order_unavailable_dish_rejected(_session_factory, client, auth_headers):
    snap = await _seed_dish_selfref(_session_factory, stock=50.0, price=20.0, qty=0.5)
    async with _session_factory() as s:
        await s.execute(text("UPDATE dishes SET available=0 WHERE id=:d"), {"d": snap["dish_id"]})
        await s.commit()
    try:
        r = await _create_order(client, _auth_headers_with_idem(auth_headers), snap["dish_id"], 2)
        assert r.status_code == 400, r.text
        async with _session_factory() as s:
            # 零落库：无流水、无该菜销售明细、库存不变
            assert (await s.execute(text(
                "SELECT COUNT(*) FROM stock_movements WHERE ingredient_id=:i"), {"i": snap["ingredient_id"]})).scalar_one() == 0
            assert (await s.execute(text(
                "SELECT COUNT(*) FROM sales_order_items WHERE dish_id=:d"), {"d": snap["dish_id"]})).scalar_one() == 0
            assert await _stock_of(s, snap["ingredient_id"]) == pytest.approx(50.0)
    finally:
        await _cleanup_dish_order(_session_factory, snap)


# ---------- 3b. RBAC：purchaser 无权访问点单域 ----------

async def test_sales_rbac_denies_purchaser(client, auth_headers):
    # purchaser 不在点单角色（order_clerk/manager）内 → 403
    r = await client.get("/api/v1/sales/menu", headers=auth_headers("purchaser"))
    assert r.status_code == 403, r.text
    # order_clerk / manager 通过
    assert (await client.get("/api/v1/sales/menu", headers=auth_headers("order_clerk"))).status_code == 200
    assert (await client.get("/api/v1/sales/menu", headers=auth_headers("manager"))).status_code == 200


# ---------- 4. 菜单 + 历史只读 ----------

async def test_sales_menu_and_history(_session_factory, client, auth_headers):
    snap = await _seed_dish_selfref(_session_factory, stock=50.0, price=20.0, qty=0.5)
    try:
        r = await client.get("/api/v1/sales/menu", headers=auth_headers("order_clerk"))
        assert r.status_code == 200, r.text
        assert any(d["dish_id"] == snap["dish_id"] and d["price"] == 20.0 for d in r.json())

        r2 = await _create_order(client, _auth_headers_with_idem(auth_headers), snap["dish_id"], 1)
        assert r2.status_code == 200
        oid = r2.json()["order_id"]

        h = await client.get("/api/v1/sales/orders", headers=auth_headers("order_clerk"))
        assert h.status_code == 200
        hist = h.json()
        assert "virtual_date" in hist and isinstance(hist["orders"], list)
        assert any(o["order_id"] == oid for o in hist["orders"])
    finally:
        await _cleanup_dish_order(_session_factory, snap)
