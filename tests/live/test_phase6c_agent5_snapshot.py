"""Phase 6-C：Agent5 快照持久化 + Dashboard 恢复（LIVE DB/Redis）。

- REVIEW→SUSPENDED：PO 的 agent5_* 与 interrupt canonical 一致（auto/manual）
- Dashboard：MySQL agent5_* 为 authoritative；为空才只读回退 checkpoint
- no_llm：Agent5 走 fallback(UNKNOWN) canonical，快速且确定
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
_CANONICAL = {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}


@pytest_asyncio.fixture
async def app_client(_session_factory):
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


async def _db_snapshot(s, order_id):
    r = (await s.execute(text(
        """SELECT agent5_summary, agent5_risk_level, agent5_risk_analysis, agent5_recommendation
           FROM purchase_orders WHERE id=:o"""), {"o": order_id})).mappings().one()
    return {"summary": r["agent5_summary"], "risk_level": r["agent5_risk_level"],
            "risk_analysis": r["agent5_risk_analysis"], "recommendation": r["agent5_recommendation"]}


# ---------- 1. Manual REVIEW → SUSPENDED：快照与 interrupt canonical 一致 ----------

async def test_manual_review_snapshot_persisted(db, seed_ingredient, app_client, no_llm, _session_factory):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    key = "key-6c-" + uuid.uuid4().hex[:8]
    r = await app_client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                              headers={"X-Idempotency-Key": key})
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED", r.text
    payload = r.json()["interrupt"]["agent5_analysis"]
    assert payload["risk_level"] == "UNKNOWN"          # no_llm → fallback canonical
    oid = r.json()["order_id"]

    async with _session_factory() as s:
        snap = await _db_snapshot(s, oid)
    for k in ("summary", "risk_level", "risk_analysis", "recommendation"):
        assert snap[k] == payload[k], f"snapshot.{k} != payload.{k}"
    assert snap["risk_level"] in _CANONICAL


# ---------- 2. Dashboard：MySQL authoritative；为空只读回退 checkpoint ----------

async def test_dashboard_mysql_authoritative_and_checkpoint_fallback(
        db, seed_ingredient, app_client, graph, no_llm, _session_factory):
    ing = await seed_ingredient(db, stock=10, daily=50, safety=30, price=32, hist=25)
    key = "key-6c2-" + uuid.uuid4().hex[:8]
    r = await app_client.post("/api/v1/purchase", json={"ingredient": ing["name"]},
                              headers={"X-Idempotency-Key": key})
    oid1 = r.json()["order_id"]
    assert r.json()["status"] == "SUSPENDED"

    dash = await app_client.get("/api/v1/dashboard")
    assert dash.status_code == 200
    orders = {o["order_id"]: o for o in dash.json()["orders"]}
    async with _session_factory() as s:
        snap = await _db_snapshot(s, oid1)
    # (a) MySQL authoritative：Dashboard 返回 = MySQL 快照
    assert orders[oid1]["agent5_analysis"] == snap

    # (b) fallback：构造"旧式"单 —— agent5_* 为空但有 checkpoint。
    #     thread_id 全局 UNIQUE，须用新 thread，并真实跑一次 REVIEW 生成其 checkpoint。
    order_no = "PO-" + uuid.uuid4().hex[:16].upper()
    thread2 = "legacy-" + uuid.uuid4().hex
    async with _session_factory() as s:
        await s.execute(text(
            """INSERT INTO purchase_orders (order_no, thread_id, status, source)
               VALUES (:o, :t, 'SUSPENDED', 'MANUAL')"""), {"o": order_no, "t": thread2})
        oid2 = int((await s.execute(text("SELECT id FROM purchase_orders WHERE order_no=:o"), {"o": order_no})).scalar_one())
        await s.commit()
    st = {"order_id": oid2, "order_no": order_no, "thread_id": thread2, "status": "RUNNING",
          "ingredient_id": ing["ingredient_id"], "ingredient": ing["name"], "unit": "kg",
          "current_stock": 10.0, "safety_stock": 30.0, "daily_sales": 50.0,
          "quantity": 0.0, "predicted_demand": 0.0, "supplier_id": ing["supplier_id"],
          "supplier_name": ing["name"] + "供应商", "supplier_price": 32.0,
          "historical_price": 25.0, "price_deviation": 0.0, "total_amount": 0.0,
          "risk_reason": None, "approved": None}
    intr = await graph.ainvoke(st, config={"configurable": {"thread_id": thread2}})
    assert "__interrupt__" in intr                    # REVIEW 写入该 thread 的 checkpoint

    dash2 = (await app_client.get("/api/v1/dashboard")).json()
    o2 = next(o for o in dash2["orders"] if o["order_id"] == oid2)
    assert o2["agent5_analysis"] is not None          # MySQL 空 → checkpoint 只读 fallback
    assert o2["agent5_analysis"]["risk_level"] == "UNKNOWN"


# ---------- 3. Auto REVIEW → SUSPENDED：快照同样落库 ----------

async def test_auto_review_snapshot_persisted(_session_factory, graph, seed_ingredient, no_llm):
    async with _session_factory() as s:
        ing = await seed_ingredient(s, stock=10, daily=50, safety=30, price=32, hist=25)

    async with _session_factory() as s:
        await scan_and_trigger_procurement(s, graph)

    # 全量会话可能有其它历史候选被触发；只断言本用例食材自身恰有一次 SUSPENDED 且快照完整
    async with _session_factory() as s:
        row = (await s.execute(text(
            """SELECT po.id, po.status FROM purchase_orders po
               JOIN purchase_order_items poi ON poi.order_id = po.id
               WHERE poi.ingredient_id = :i AND po.status = 'SUSPENDED'
               ORDER BY po.id DESC LIMIT 1"""), {"i": ing["ingredient_id"]})).mappings().first()
        assert row is not None, "本食材应被 auto scan 触发为 SUSPENDED"
        snap = await _db_snapshot(s, row["id"])
    assert snap["risk_level"] == "UNKNOWN" and snap["risk_level"] in _CANONICAL
    assert snap["summary"] is not None               # 完整快照（summary 来自 fallback 文案）
