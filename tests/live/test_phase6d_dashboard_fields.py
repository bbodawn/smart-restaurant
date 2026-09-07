"""Phase 6-D：Dashboard 展示层收口（LIVE）。

验证 orders response 能区分 AUTO / MANUAL(人工审批)：
- AUTO 订单：source/supplier_name/unit_price/金额/状态完整
- REVIEW(MANUAL) 订单：source/supplier/unit_price/agent5/审批字段完整
"""
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from app.core.db import get_db
from app.services.auto_procurement import scan_and_trigger_procurement

pytestmark = pytest.mark.live
_REDIS = "redis://127.0.0.1:6379/0"


@pytest_asyncio.fixture
async def dash_client(_session_factory):
    from app.api.dashboard import router as dash_router
    from app.api.purchase import router as purchase_router
    from app.graph.workflow import build_graph
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    app = FastAPI()
    async with AsyncRedisSaver.from_conn_string(_REDIS) as saver:
        await saver.asetup()
        app.state.graph = build_graph(saver)
        app.include_router(purchase_router, prefix="/api/v1")
        app.include_router(dash_router, prefix="/api/v1")

        async def _override():
            async with _session_factory() as s:
                yield s

        app.dependency_overrides[get_db] = _override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client


async def _get_order(client, order_id):
    r = await client.get("/api/v1/dashboard")
    assert r.status_code == 200
    for o in r.json()["orders"]:
        if o["order_id"] == order_id:
            return o
    raise AssertionError(f"order {order_id} not in dashboard")


# ---------- AUTO 订单字段完整 ----------

async def test_dashboard_auto_order_fields(_session_factory, graph, seed_ingredient, dash_client):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)  # PURCHASE 自动
    async with _session_factory() as s:
        triggered = await scan_and_trigger_procurement(s, graph)
    oid = next(t["order_id"] for t in triggered if t["ingredient"] == ing["name"])

    o = await _get_order(dash_client, oid)
    assert o["source"] == "AUTO"
    assert o["supplier_name"] is not None
    assert o["unit_price"] == 5.0
    assert o["quantity"] == 90.0
    assert o["total_amount"] == 450.0
    assert o["status"] == "COMPLETED"


# ---------- 人工审批(MANUAL) 订单：SUSPENDED → approve → 字段完整 ----------

async def test_dashboard_review_manual_fields(db, seed_ingredient, dash_client, no_llm):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)  # REVIEW
    key = "key-6d-" + uuid.uuid4().hex[:8]
    r = await dash_client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                               headers={"X-Idempotency-Key": key})
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    oid = r.json()["order_id"]

    o = await _get_order(dash_client, oid)
    assert o["source"] == "MANUAL"
    assert o["supplier_name"] is not None
    assert o["unit_price"] == 32.0
    assert o["status"] == "SUSPENDED"
    a5 = o["agent5_analysis"]
    assert isinstance(a5, dict) and a5["risk_level"] == "UNKNOWN"  # no_llm → canonical fallback

    ar = await dash_client.post(f"/api/v1/purchase/{oid}/approve",
                                json={"approved": True, "approval_reason": "phase6d-ok"})
    assert ar.status_code == 200 and ar.json()["status"] == "COMPLETED"

    o2 = await _get_order(dash_client, oid)
    assert o2["status"] == "COMPLETED"
    assert o2["source"] == "MANUAL"
    assert o2["approval_reason"] == "phase6d-ok"
    assert o2["approved_virtual_date"] is not None
    assert o2["agent5_analysis"]["risk_level"] == "UNKNOWN"
