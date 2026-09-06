"""Phase 6-B-2：Manual Purchase Flow 闭环（LIVE HTTP E2E）。

覆盖：Manual NO_PURCHASE(不建单) / PURCHASE(完整闭环) / 同 key 幂等 /
并发不同 key 去重 / REVIEW approve / REVIEW reject / duplicate approve /
legacy /approve 已删除。Auto / 6-B-1 / 5-B 回归由同套件其它文件覆盖。
"""
import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from app.core.db import get_db

pytestmark = pytest.mark.live
_REDIS = "redis://127.0.0.1:6379/0"


@pytest_asyncio.fixture
async def manual_client(_session_factory):
    """真实 Redis checkpoint 图 + purchase 路由，DB 依赖指向 restaurant_it。"""
    from app.api.purchase import router as purchase_router
    from app.graph.workflow import build_graph
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    app = FastAPI()
    async with AsyncRedisSaver.from_conn_string(_REDIS) as saver:
        await saver.asetup()
        app.state.graph = build_graph(saver)
        app.include_router(purchase_router, prefix="/api/v1")

        async def _override():
            async with _session_factory() as s:
                yield s

        app.dependency_overrides[get_db] = _override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client


async def _post(client, name, key):
    return await client.post("/api/v1/purchase", json={"ingredient": name},
                             headers={"X-Idempotency-Key": key})


async def _count(s, sql, **p):
    return int((await s.execute(text(sql), p)).scalar_one())


async def _stock(s, ing_id):
    return float((await s.execute(text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ing_id})).scalar_one())


# ---------- 1. Manual NO_PURCHASE：不建单 ----------

async def test_manual_no_purchase_creates_nothing(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=100, daily=10, safety=20, price=5, hist=5)  # 健康库存→NO_PURCHASE
    async with _session_factory() as s:
        orders_before = await _count(s, "SELECT COUNT(*) FROM purchase_orders")

    r = await _post(manual_client, ing["name"], "key-nop-" + uuid.uuid4().hex[:8])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "NO_PURCHASE"

    async with _session_factory() as s:
        assert await _count(s, "SELECT COUNT(*) FROM purchase_orders") == orders_before  # 未新增 PO
        assert await _stock(s, ing["ingredient_id"]) == 100.0
        # 该食材无任何 item → 必无 inbound（NO_PURCHASE 不应有 PO/PO Item）
        assert await _count(s, "SELECT COUNT(*) FROM purchase_order_items poi JOIN purchase_orders po ON po.id=poi.order_id WHERE poi.ingredient_id=:i", i=ing["ingredient_id"]) == 0
        assert await _count(s, """SELECT COUNT(*) FROM inbound_records ir
                                  JOIN purchase_order_items poi ON poi.id=ir.order_item_id
                                  WHERE poi.ingredient_id=:i""", i=ing["ingredient_id"]) == 0


# ---------- 2. Manual PURCHASE：PO→item→inbound→inventory→COMPLETED ----------

async def test_manual_purchase_full_closure(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=60, daily=50, safety=30, price=5, hist=5)  # PURCHASE qty=90 total=450
    async with _session_factory() as s:
        orders_before = await _count(s, "SELECT COUNT(*) FROM purchase_orders")

    r = await _post(manual_client, ing["name"], "key-p-" + uuid.uuid4().hex[:8])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "COMPLETED"
    assert body["policy_decision"]["quantity"] == 90.0
    assert body["policy_decision"]["total_amount"] == 450.0   # 来自 policy_decision 而非顶层(0)
    assert body["restocked_quantity"] == 90.0

    async with _session_factory() as s:
        assert await _count(s, "SELECT COUNT(*) FROM purchase_orders") == orders_before + 1
        po = (await s.execute(text(
            "SELECT id, status, source, total_amount FROM purchase_orders WHERE id = :o"),
            {"o": body["order_id"]})).mappings().one()
        assert po["status"] == "COMPLETED" and po["source"] == "MANUAL"
        assert float(po["total_amount"]) == 450.0
        item_qty = float((await s.execute(text(
            "SELECT quantity FROM purchase_order_items WHERE order_id=:o"), {"o": body["order_id"]})).scalar_one())
        assert item_qty == 90.0
        assert await _count(s, "SELECT COUNT(*) FROM inbound_records WHERE order_id=:o", o=body["order_id"]) == 1
        assert await _stock(s, ing["ingredient_id"]) == 150.0


# ---------- 3. Manual 同 key 重复：不重复建单/入库 ----------

async def test_manual_same_key_replay(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=60, daily=50, safety=30, price=5, hist=5)
    key = "key-replay-" + uuid.uuid4().hex[:8]

    r1 = await _post(manual_client, ing["name"], key)
    assert r1.status_code == 200 and r1.json()["status"] == "COMPLETED"
    r2 = await _post(manual_client, ing["name"], key)
    assert r2.status_code == 200
    assert r2.json()["order_id"] == r1.json()["order_id"]

    async with _session_factory() as s:
        assert await _count(s, "SELECT COUNT(*) FROM inbound_records WHERE order_id=:o", o=r1.json()["order_id"]) == 1
        assert await _stock(s, ing["ingredient_id"]) == 150.0  # 只入库一次


# ---------- 4. Manual 并发不同 key：同需求只建一单 ----------

async def test_manual_concurrent_different_keys(_session_factory, seed_ingredient, manual_client):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)

    ra, rb = await asyncio.gather(
        _post(manual_client, ing["name"], "key-c-a-" + uuid.uuid4().hex[:6]),
        _post(manual_client, ing["name"], "key-c-b-" + uuid.uuid4().hex[:6]),
    )

    def _kind(r):
        if r.status_code == 409:
            return "409"
        if r.status_code == 200:
            return r.json().get("status", "?")
        return f"HTTP{r.status_code}"

    kinds = [_kind(ra), _kind(rb)]
    assert kinds.count("COMPLETED") == 1, kinds   # 至多一单完成
    assert all(k in ("COMPLETED", "NO_PURCHASE", "409") for k in kinds), kinds

    async with _session_factory() as s:
        completed = (await s.execute(text(
            """SELECT COUNT(DISTINCT po.id) FROM purchase_orders po
               JOIN purchase_order_items poi ON poi.order_id=po.id
               WHERE poi.ingredient_id=:i AND po.status='COMPLETED'"""),
            {"i": ing["ingredient_id"]})).scalar_one()
        assert completed == 1
        assert await _count(s, """SELECT COUNT(*) FROM inbound_records ir
                                  JOIN purchase_order_items poi ON poi.id=ir.order_item_id
                                  WHERE poi.ingredient_id=:i""", i=ing["ingredient_id"]) == 1
        assert await _stock(s, ing["ingredient_id"]) == 150.0


# ---------- 5. Manual REVIEW → approve ----------

async def test_manual_review_approve(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)  # REVIEW
    r = await _post(manual_client, ing["name"], "key-rv-a-" + uuid.uuid4().hex[:6])
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    oid = r.json()["order_id"]

    ar = await manual_client.post(f"/api/v1/purchase/{oid}/approve",
                                  json={"approved": True, "approval_reason": "ok"})
    assert ar.status_code == 200 and ar.json()["status"] == "COMPLETED"

    async with _session_factory() as s:
        assert await _stock(s, ing["ingredient_id"]) == 150.0
        assert await _count(s, "SELECT COUNT(*) FROM inbound_records WHERE order_id=:o", o=oid) == 1
        st = (await s.execute(text("SELECT status, approved_virtual_date FROM purchase_orders WHERE id=:o"), {"o": oid})).mappings().one()
        assert st["status"] == "COMPLETED" and st["approved_virtual_date"] is not None


# ---------- 6. Manual REVIEW → reject ----------

async def test_manual_review_reject(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    r = await _post(manual_client, ing["name"], "key-rv-b-" + uuid.uuid4().hex[:6])
    oid = r.json()["order_id"]
    assert r.json()["status"] == "SUSPENDED"

    rr = await manual_client.post(f"/api/v1/purchase/{oid}/approve",
                                  json={"approved": False, "approval_reason": "no"})
    assert rr.status_code == 200 and rr.json()["status"] == "REJECTED"

    async with _session_factory() as s:
        assert await _stock(s, ing["ingredient_id"]) == 10.0   # 不变
        assert await _count(s, "SELECT COUNT(*) FROM inbound_records WHERE order_id=:o", o=oid) == 0
        st = (await s.execute(text("SELECT status, rejected_virtual_date FROM purchase_orders WHERE id=:o"), {"o": oid})).mappings().one()
        assert st["status"] == "REJECTED" and st["rejected_virtual_date"] is not None


# ---------- 7. Duplicate approve：不重复入库 ----------

async def test_manual_duplicate_approve(db, seed_ingredient, manual_client, _session_factory):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    r = await _post(manual_client, ing["name"], "key-dp-" + uuid.uuid4().hex[:6])
    oid = r.json()["order_id"]

    a1 = await manual_client.post(f"/api/v1/purchase/{oid}/approve", json={"approved": True})
    a2 = await manual_client.post(f"/api/v1/purchase/{oid}/approve", json={"approved": True})
    assert a1.json()["status"] == "COMPLETED" and a2.status_code == 200
    assert a2.json()["status"] == "COMPLETED" and "message" in a2.json()

    async with _session_factory() as s:
        assert await _stock(s, ing["ingredient_id"]) == 150.0
        assert await _count(s, "SELECT COUNT(*) FROM inbound_records WHERE order_id=:o", o=oid) == 1


# ---------- 8. Legacy /approve/{id} 已删除 ----------

async def test_legacy_approve_route_removed(manual_client):
    r = await manual_client.post("/api/v1/approve/1", json={"approved": True})
    assert r.status_code in (404, 405)  # 路由不存在
