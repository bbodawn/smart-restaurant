"""Phase 8：/inbound/orders 采购订单展示 API（LIVE）。

覆盖派生规则：AUTO COMPLETED / REVIEW SUSPENDED / approve / reject 的
approval_status·reviewer_display·inbound_status 与 Agent5 信息。
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
async def client(_session_factory):
    from app.api.inbound import router as inbound_router
    from app.api.purchase import router as purchase_router
    from app.graph.workflow import build_graph
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    app = FastAPI()
    async with AsyncRedisSaver.from_conn_string(_REDIS) as saver:
        await saver.asetup()
        app.state.graph = build_graph(saver)
        app.include_router(inbound_router, prefix="/api/v1")
        app.include_router(purchase_router, prefix="/api/v1")

        async def _override():
            async with _session_factory() as s:
                yield s

        app.dependency_overrides[get_db] = _override
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _order_by_ingredient(client, ingredient_name):
    r = await client.get("/api/v1/inbound/orders")
    assert r.status_code == 200
    for o in r.json():
        if o["ingredient"] == ingredient_name:
            return o
    raise AssertionError(f"{ingredient_name} not in inbound orders")


# ---------- 1. AUTO COMPLETED ----------

async def test_auto_completed_fields(_session_factory, graph, seed_ingredient, client):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=60, daily=50, safety=30, price=5, hist=5)
    async with _session_factory() as s:
        await scan_and_trigger_procurement(s, graph)

    o = await _order_by_ingredient(client, ing["name"])
    assert o["source"] == "AUTO" and o["status"] == "COMPLETED"
    assert o["approval_status"] == "自动审核"
    assert o["reviewer_display"] == "系统"
    assert o["inbound_status"] == "已入库"
    assert o["unit_price"] == 5.0 and o["total_amount"] == 450.0
    assert o["supplier_name"] is not None


# ---------- 2. REVIEW SUSPENDED ----------

async def test_review_suspended_fields(db, seed_ingredient, client, no_llm):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    key = "key-p8a-" + uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                          headers={"X-Idempotency-Key": key})
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"

    o = await _order_by_ingredient(client, ing["name"])
    assert o["approval_status"] == "等待审核"
    assert o["reviewer_display"] == "-"
    assert o["inbound_status"] == "待审核"
    assert isinstance(o["agent5_analysis"], dict)
    assert o["agent5_analysis"]["risk_level"] == "UNKNOWN"  # no_llm canonical fallback


# ---------- 3. approve 后 ----------

async def test_approve_after_fields(db, seed_ingredient, client, no_llm, auth_headers):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    key = "key-p8b-" + uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                          headers={"X-Idempotency-Key": key})
    oid = r.json()["order_id"]
    ar = await client.post(f"/api/v1/purchase/{oid}/approve",
                           json={"approved": True, "approval_reason": "phase8-ok"},
                           headers=auth_headers("purchaser"))
    assert ar.json()["status"] == "COMPLETED"

    d = (await client.get(f"/api/v1/inbound/orders/{oid}")).json()
    assert d["status"] == "COMPLETED"
    assert d["approval_status"] == "已通过"
    assert d["reviewer_display"] == "人工审核"
    assert d["approval_reason"] == "phase8-ok"
    assert d["approved_virtual_date"] is not None
    assert d["inbound_status"] == "已入库"


# ---------- 4. reject 后 ----------

async def test_reject_after_fields(db, seed_ingredient, client, no_llm, auth_headers):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    key = "key-p8c-" + uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                          headers={"X-Idempotency-Key": key})
    oid = r.json()["order_id"]
    rr = await client.post(f"/api/v1/purchase/{oid}/approve",
                           json={"approved": False, "approval_reason": "phase8-no"},
                           headers=auth_headers("purchaser"))
    assert rr.json()["status"] == "REJECTED"

    d = (await client.get(f"/api/v1/inbound/orders/{oid}")).json()
    assert d["status"] == "REJECTED"
    assert d["approval_status"] == "已拒绝"
    assert d["reviewer_display"] == "人工审核"
    assert d["inbound_status"] == "已拒绝"
    assert d["rejected_virtual_date"] is not None
