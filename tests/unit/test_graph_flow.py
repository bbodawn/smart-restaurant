"""Graph 拓扑 / 路由契约（InMemorySaver，MOCK TEST）。

验证：三分析并行 fan-out 后收敛到 deterministic_policy（fan-in 只跑一次）；
Policy 三态路由到正确终态。不依赖任何外部服务。
"""
import uuid

import pytest


def _state(cur, daily, safety, price, hist):
    """与 API 真实入口一致：只给顶层字段，由分析节点算出 analysis_*。"""
    return {
        "order_id": 1,
        "order_no": "PO-T",
        "thread_id": f"graph-{uuid.uuid4().hex}",
        "ingredient_id": 3,
        "ingredient": "优质猪肉",
        "unit": "kg",
        "current_stock": cur,
        "safety_stock": safety,
        "daily_sales": daily,
        "predicted_demand": 0.0,
        "quantity": 0.0,
        "supplier_id": 3,
        "supplier_name": "华东肉类供应商",
        "supplier_price": price,
        "historical_price": hist,
        "status": "RUNNING",
    }


async def test_parallel_analyses_converge_once_into_policy(counting_graph, no_llm):
    """契约：一次 REVIEW 执行，三个分析节点各跑一次、deterministic_policy 只跑一次
    （fan-in 收敛，不重复执行）；policy 能看到三者事实并判定 REVIEW。"""
    graph, counters, _ = counting_graph
    st = _state(10, 10, 12, 30, 20)  # 库存<=安全线 + 价格偏离>20% → REVIEW
    config = {"configurable": {"thread_id": st["thread_id"]}}
    result = await graph.ainvoke(st, config=config)

    assert "__interrupt__" in result                      # REVIEW 走到 HITL interrupt
    assert counters["inventory_analysis_node"] == 1
    assert counters["price_analysis_node"] == 1
    assert counters["supplier_analysis_node"] == 1
    assert counters["deterministic_policy_node"] == 1     # fan-in 只执行一次
    # 三个事实都写入了本次执行 state
    assert result.get("analysis_inventory", {}).get("health_status") == "BELOW_SAFETY"
    assert result.get("analysis_price", {}).get("price_status") == "GT_20PCT"
    assert result.get("analysis_supplier", {}).get("supplier_name") == "华东肉类供应商"
    assert result.get("policy_decision", {}).get("status") == "REVIEW"


async def test_route_no_purchase_ends_without_executing_purchase(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _state(50, 10, 12, 5, 5)  # 健康库存
    result = await graph.ainvoke(st, config={"configurable": {"thread_id": st["thread_id"]}})

    assert "__interrupt__" not in result
    assert result["policy_decision"]["status"] == "NO_PURCHASE"
    assert counters["purchase_prepare"] == 0
    assert counters["purchase_agent"] == 0


async def test_route_purchase_goes_through_prepare_then_agent(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _state(20, 10, 12, 5, 5)  # 低于3日需求、无风险 → PURCHASE
    result = await graph.ainvoke(st, config={"configurable": {"thread_id": st["thread_id"]}})

    assert "__interrupt__" not in result
    assert result["policy_decision"]["status"] == "PURCHASE"
    assert counters["purchase_prepare"] == 1
    assert counters["purchase_agent"] == 1
    assert result["status"] == "PURCHASE_CREATED"         # 顶态由 purchase_agent 归一


async def test_route_review_goes_agent5_then_hitl_interrupt(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _state(10, 10, 12, 5, 5)  # 仅库存风险 → REVIEW
    result = await graph.ainvoke(st, config={"configurable": {"thread_id": st["thread_id"]}})

    assert counters["agent5_node"] == 1                   # REVIEW 必经 Agent5
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "PURCHASE_REVIEW_APPROVAL"
    assert payload["quantity"] == 20.0                    # policy 数量透传给人
