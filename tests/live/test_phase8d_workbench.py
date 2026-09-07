"""Phase 8-D：采购审核工作台动作验证（LIVE）。

工作台"批准/拒绝"复用既有 POST /purchase/{id}/approve（approved true/false）。
验证：
- approve(true)：SUSPENDED -> COMPLETED；库存增加；inbound_records 生成 1 条
- approve(false)：SUSPENDED -> REJECTED；库存不增加；无 inbound
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
_REDIS = "redis://127.0.0.1:6379/0"


@pytest_asyncio.fixture
async def client(_session_factory):
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
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _stock(s, ing_id):
    return float((await s.execute(text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"), {"i": ing_id})).scalar_one())


async def _new_suspended(_session_factory, seed_ingredient):
    """构造一个 SUSPENDED(manual REVIEW) 订单，返回 (ingredient, order_id)。"""
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=10, daily=50, safety=30, price=32, hist=25)
    return ing


# approve(true)：COMPLETED + 库存增加 + inbound 1
async def test_workbench_approve(_session_factory, seed_ingredient, client, no_llm):
    ing = await _new_suspended(_session_factory, seed_ingredient)
    key = "w8d-a-" + uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                          headers={"X-Idempotency-Key": key})
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    oid = r.json()["order_id"]
    async with _session_factory() as s:
        before = await _stock(s, ing["ingredient_id"])

    ar = await client.post(f"/api/v1/purchase/{oid}/approve",
                           json={"approved": True, "approval_reason": "工作台批准"})
    assert ar.status_code == 200 and ar.json()["status"] == "COMPLETED"

    async with _session_factory() as s:
        assert await _stock(s, ing["ingredient_id"]) == pytest.approx(before + 140.0)
        cnt = int((await s.execute(text("SELECT COUNT(*) FROM inbound_records WHERE order_id=:o"), {"o": oid})).scalar_one())
        assert cnt == 1
        st = (await s.execute(text("SELECT status FROM purchase_orders WHERE id=:o"), {"o": oid})).scalar_one()
        assert st == "COMPLETED"


# approve(false)：REJECTED + 库存不变 + 无 inbound
async def test_workbench_reject(_session_factory, seed_ingredient, client, no_llm):
    ing = await _new_suspended(_session_factory, seed_ingredient)
    key = "w8d-r-" + uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                          headers={"X-Idempotency-Key": key})
    oid = r.json()["order_id"]
    async with _session_factory() as s:
        before = await _stock(s, ing["ingredient_id"])

    rr = await client.post(f"/api/v1/purchase/{oid}/approve",
                           json={"approved": False, "approval_reason": "工作台驳回"})
    assert rr.status_code == 200 and rr.json()["status"] == "REJECTED"

    async with _session_factory() as s:
        assert await _stock(s, ing["ingredient_id"]) == pytest.approx(before)  # 库存不增加
        cnt = int((await s.execute(text("SELECT COUNT(*) FROM inbound_records WHERE order_id=:o"), {"o": oid})).scalar_one())
        assert cnt == 0
        st = (await s.execute(text("SELECT status FROM purchase_orders WHERE id=:o"), {"o": oid})).scalar_one()
        assert st == "REJECTED"
