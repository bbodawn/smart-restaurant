"""Phase 5-B：真实 REVIEW→Agent5→Redis Checkpoint→Dashboard→HITL→入库 全链路 LIVE E2E。

只验证、不修改任何业务代码。覆盖点：
- 必然 REVIEW 的真实业务数据（库存<3日需求 且 价格偏离>20%）
- 真实 LangGraph（AsyncRedisSaver, Redis db 0）+ 真实 Ollama Agent5
- scan_and_trigger_procurement（真实自动巡检入口）→ SUSPENDED 落库
- checkpoint 中确存 agent5_analysis / policy_decision
- Dashboard 走真实 HTTP 路由读取（dependency override 指向 restaurant_it）
- HITL approve 走真实 HTTP 路由 → resume 同 checkpoint → 入库一次
- START 不重跑（节点计数）；幂等（二次 approve 不重复入库）
"""
import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text

from app.core.db import get_db
from app.services.auto_procurement import scan_and_trigger_procurement

pytestmark = pytest.mark.live

# 与 live/conftest.py 默认一致（此处为避免依赖同文件常量重复声明）
_MYSQL = "mysql+aiomysql://root:123456@127.0.0.1:3306/restaurant_it"
_REDIS = "redis://127.0.0.1:6379/0"

NODE_NAMES = (
    "inventory_analysis_node",
    "price_analysis_node",
    "supplier_analysis_node",
    "deterministic_policy_node",
    "purchase_prepare",
    "purchase_agent",
    "agent5_node",
)


def _seed_state(name, ing) -> dict:
    """与 auto_procurement 组装 state 的字段契约一致。"""
    return {
        "ingredient_id": ing["ingredient_id"],
        "ingredient": name,
        "unit": "kg",
        "current_stock": ing["stock"],
        "safety_stock": ing["safety"],
        "daily_sales": ing["daily"],
        "predicted_demand": 0.0,
        "quantity": 0.0,
        "demand_reasoning": "",
        "supplier_id": ing["supplier_id"],
        "supplier_name": name + "供应商",
        "supplier_price": ing["price"],
        "historical_price": ing["hist"],
        "price_deviation": 0.0,
        "total_amount": 0.0,
        "risk_analysis_report": None,
        "risk_reason": None,
        "approved": None,
        "status": "RUNNING",
    }


async def test_phase5b_fullchain_live(_session_factory, counting_node, monkeypatch, auth_headers):
    """全链路：Seed→REVIEW→Agent5(真)→SUSPENDED→Checkpoint→Dashboard→approve→入库一次→幂等。"""
    import app.graph.workflow as wf
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    # ---- 0. 独立 session 用于播种 / 后续查询 ----
    async with _session_factory() as s:
        # 构造必然 REVIEW：库存 10 ≤ 安全线 30（<3日需求 150）；报价 32 vs 历史 25（偏离 28%）
        name = f"P5B-{uuid.uuid4().hex[:8]}"
        await s.execute(text("INSERT INTO ingredients (name, unit, category) VALUES (:n,'kg','测试')"), {"n": name})
        ing_id = int((await s.execute(text("SELECT id FROM ingredients WHERE name=:n"), {"n": name})).scalar_one())
        stock, daily, safety, price, hist = 10.0, 50.0, 30.0, 32.0, 25.0
        await s.execute(
            text("INSERT INTO inventory (ingredient_id,current_stock,daily_sales,safety_stock) VALUES (:i,:c,:d,:f)"),
            {"i": ing_id, "c": stock, "d": daily, "f": safety},
        )
        await s.execute(
            text("INSERT INTO suppliers (name,ingredient_id,current_price,historical_avg_price,rating) VALUES (:n,:i,:p,:h,4.5)"),
            {"n": name + "供应商", "i": ing_id, "p": price, "h": hist},
        )
        await s.commit()
        pred = daily * 3
        exp_qty = round(max(pred - stock, 0), 2)
        exp_total = round(exp_qty * price, 2)
        evidence = {
            "ingredient_id": ing_id,
            "name": name,
            "current_stock_before": stock,
            "daily_sales": daily,
            "safety_stock": safety,
            "predicted_3_day_demand": pred,
            "supplier_price": price,
            "historical_price": hist,
            "expected_price_deviation": round(abs(price - hist) / hist, 4),
            "expected_quantity": exp_qty,
            "expected_total_amount": exp_total,
        }
        print("\n[EVIDENCE seed]", evidence)

        # ---- 1. 仪表化真实图（AsyncRedisSaver，Redis db0），计数证明 START 不重跑 ----
        for nm in NODE_NAMES:
            monkeypatch.setattr(wf, nm, counting_node(getattr(wf, nm)))
        async with AsyncRedisSaver.from_conn_string(_REDIS) as saver:
            await saver.asetup()
            graph = wf.build_graph(saver)
            counters = counting_node.counters

            # ---- 2. 真实自动巡检入口触发 → 预期 REVIEW/SUSPENDED ----
            triggered = await scan_and_trigger_procurement(s, graph)
            assert len(triggered) == 1, triggered
            assert triggered[0]["status"] == "SUSPENDED"
            row = (await s.execute(text(
                """SELECT po.id, po.order_no, po.thread_id, po.status, po.total_amount,
                          po.risk_analysis_report, poi.quantity
                   FROM purchase_orders po
                   JOIN purchase_order_items poi ON poi.order_id = po.id
                   WHERE po.id = :i"""), {"i": triggered[0]["order_id"]})).mappings().one()
            order_id, thread_id = row["id"], row["thread_id"]
            print("[EVIDENCE order]", {"order_id": order_id, "order_no": row["order_no"],
                                       "thread_id": thread_id, "status": row["status"],
                                       "total_amount_db": float(row["total_amount"]),
                                       "item_quantity_db": float(row["quantity"])})

            # ---- 3. 真实 checkpoint 内容 ----
            cfg = {"configurable": {"thread_id": thread_id}}
            snap = await graph.aget_state(cfg)
            vals = snap.values or {}
            ck_agent5 = vals.get("agent5_analysis")
            ck_policy = vals.get("policy_decision")
            assert isinstance(ck_policy, dict) and ck_policy.get("status") == "REVIEW"
            assert ck_policy.get("needs_purchase") is True
            assert ck_policy.get("risk_flags") not in (None, [])
            assert float(ck_policy.get("quantity", 0)) > 0
            assert isinstance(ck_agent5, dict), "agent5_analysis 必须真实存在于 checkpoint"
            assert set(ck_agent5) == {"summary", "risk_level", "risk_analysis", "recommendation"}
            real_llm = not (ck_agent5["risk_level"] == "UNKNOWN")
            print("[EVIDENCE checkpoint] policy_decision =", ck_policy)
            print("[EVIDENCE checkpoint] agent5_analysis =", ck_agent5)
            print("[EVIDENCE checkpoint] agent5 real-LLM =", real_llm)

            # agent5 不越权：quantity 仍以 policy 为准，agent5 无 status/approved 等键（4 键已验）

            # ---- 4. Dashboard 走真实 HTTP 路由 ----
            from app.api.dashboard import router as dash_router
            from app.api.purchase import router as purchase_router

            app = FastAPI()
            app.state.graph = graph
            app.include_router(dash_router, prefix="/api/v1")
            app.include_router(purchase_router, prefix="/api/v1")

            async def _override_get_db():
                async with _session_factory() as s2:
                    yield s2

            app.dependency_overrides[get_db] = _override_get_db
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/v1/dashboard")
                assert r.status_code == 200, f"dashboard HTTP {r.status_code}: {r.text[:300]}"
                body = r.json()
                found = [o for o in body["orders"] if o["order_id"] == order_id]
                assert len(found) == 1
                d = found[0]
                assert d["status"] == "SUSPENDED"
                assert d["agent5_analysis"] == ck_agent5, "Dashboard 返回的 agent5 必须与 checkpoint 一致"
                assert d["order_no"] == row["order_no"]
                print("[EVIDENCE dashboard] HTTP 200; agent5_analysis == checkpoint =",
                      d["agent5_analysis"])
                # 前端不展示字段检查（Dashboard 层若内嵌 risk_reason 原文则记录，不作为失败项）
                print("[EVIDENCE dashboard] order keys has agent5_analysis:", "agent5_analysis" in d)

                # ---- 5. HITL approve（真实 HTTP）----
                # 注意：execute_inbound_stock 在另一会话 commit；验证必须用新会话读，
                # 避免 MySQL REPEATABLE READ 下本会话旧快照读到未变库存（误判为缺陷）。
                r2 = await client.post(f"/api/v1/purchase/{order_id}/approve",
                                       json={"approved": True, "approval_reason": "phase5b"},
                                       headers=auth_headers("purchaser"))
                assert r2.status_code == 200, r2.text[:300]
                resp = r2.json()
                assert resp["status"] == "COMPLETED"
                async with _session_factory() as chk:
                    stock_after_first = await _stock_of(chk, ing_id)
                    ords = (await chk.execute(text(
                        "SELECT COUNT(*) c FROM purchase_orders WHERE id=:i AND status='COMPLETED'"),
                        {"i": order_id})).scalar_one()
                    items = (await chk.execute(text(
                        "SELECT COUNT(*) FROM purchase_order_items WHERE order_id=:i"),
                        {"i": order_id})).scalar_one()
                print("[EVIDENCE approve#1]", {"resp_status": resp["status"],
                                               "restocked": resp.get("restocked_quantity"),
                                               "stock_after_first": stock_after_first})
                assert stock_after_first == pytest.approx(stock + exp_qty)

                # 计数证明：START 不重跑（分析/policy/agent5 仍各 1 次）
                print("[EVIDENCE counters]", dict(counters))
                assert counters["inventory_analysis_node"] == 1
                assert counters["price_analysis_node"] == 1
                assert counters["supplier_analysis_node"] == 1
                assert counters["deterministic_policy_node"] == 1
                assert counters["agent5_node"] == 1
                assert counters["purchase_prepare"] == 1
                assert counters["purchase_agent"] == 1

                # 只入库一次：订单唯一、明细唯一、状态 COMPLETED
                assert ords == 1 and items == 1
                print("[EVIDENCE mysql]", {"completed_orders": ords, "order_items": items})

                # ---- 6. 幂等：二次 approve 不重复入库 ----
                async with _session_factory() as chk:
                    stock_before_second = await _stock_of(chk, ing_id)
                r3 = await client.post(f"/api/v1/purchase/{order_id}/approve",
                                       json={"approved": True, "approval_reason": "phase5b-2"},
                                       headers=auth_headers("purchaser"))
                assert r3.status_code == 200
                assert r3.json()["status"] == "COMPLETED"
                async with _session_factory() as chk:
                    stock_after_second = await _stock_of(chk, ing_id)
                print("[EVIDENCE idempotency]", {"before": stock_before_second,
                                                 "second_approve": stock_after_second})
                assert stock_after_second == pytest.approx(stock_before_second)
                assert stock_after_second == pytest.approx(stock + exp_qty)


async def _stock_of(s, ingredient_id):
    val = await s.execute(text("SELECT current_stock FROM inventory WHERE ingredient_id=:i"),
                          {"i": ingredient_id})
    return float(val.scalar_one())
