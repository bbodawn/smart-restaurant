"""HITL 审批语义契约（InMemorySaver，MOCK TEST）。

验证：REVIEW 真正 interrupt；approve 从断点续跑且【不重跑 START 前的分析】；
reject 直接结束、绝不进入 purchase 节点；换一个新编译的 graph、共享同一 checkpoint
仍可续跑（模拟 Redis checkpoint 的断点恢复语义）。
不依赖外部服务。
"""
import uuid

import pytest
from langgraph.types import Command

import app.graph.workflow as wf
from app.graph.nodes import purchase_approval_node  # noqa: F401 (确保模块导入)


def _review_state():
    return {
        "order_id": 1,
        "order_no": "PO-T",
        "thread_id": f"hitl-{uuid.uuid4().hex}",
        "ingredient_id": 3,
        "ingredient": "优质猪肉",
        "unit": "kg",
        "current_stock": 10,
        "safety_stock": 12,
        "daily_sales": 10,
        "predicted_demand": 0.0,
        "quantity": 0.0,
        "supplier_id": 3,
        "supplier_name": "华东肉类供应商",
        "supplier_price": 5,
        "historical_price": 5,
        "status": "RUNNING",
    }


def _config(state):
    return {"configurable": {"thread_id": state["thread_id"]}}


async def test_review_really_interrupts_with_payload(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _review_state()
    result = await graph.ainvoke(st, config=_config(st))

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "PURCHASE_REVIEW_APPROVAL"
    assert payload["quantity"] == 20.0
    # Agent5 在中断前已把可读分析放进 payload（LLM 失败→fallback 也应出现）
    assert isinstance(payload.get("agent5_analysis"), dict)
    assert set(payload["agent5_analysis"]) == {"summary", "risk_level", "risk_analysis", "recommendation"}


async def test_approve_resumes_without_rerunning_analyses(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _review_state()
    cfg = _config(st)
    await graph.ainvoke(st, config=cfg)

    # 中断前：三分析 + policy + agent5 各一次
    assert counters["inventory_analysis_node"] == 1
    assert counters["deterministic_policy_node"] == 1

    final = await graph.ainvoke(Command(resume={"approved": True}), config=cfg)

    assert final["status"] == "PURCHASE_CREATED"          # approve → prepare → agent
    # resume 不重跑 START：分析/policy/agent5 仍是 1 次
    assert counters["inventory_analysis_node"] == 1
    assert counters["price_analysis_node"] == 1
    assert counters["supplier_analysis_node"] == 1
    assert counters["deterministic_policy_node"] == 1
    assert counters["agent5_node"] == 1
    assert counters["purchase_prepare"] == 1
    assert counters["purchase_agent"] == 1


async def test_reject_ends_without_entering_purchase(counting_graph, no_llm):
    graph, counters, _ = counting_graph
    st = _review_state()
    cfg = _config(st)
    await graph.ainvoke(st, config=cfg)

    final = await graph.ainvoke(Command(resume={"approved": False}), config=cfg)

    assert counters["purchase_prepare"] == 0              # reject 绝不执行采购
    assert counters["purchase_agent"] == 0
    assert final.get("status") != "PURCHASE_CREATED"


async def test_resume_works_from_freshly_compiled_graph_shared_saver(counting_graph, no_llm):
    """模拟『服务重启后、checkpoint 仍在』：同一 saver 上重新编译一张图，也能从断点续跑。"""
    graph, _, saver = counting_graph
    st = _review_state()
    cfg = _config(st)
    await graph.ainvoke(st, config=cfg)

    # 用同一 saver 重新 build（等价于应用重启后重建 graph）
    graph2 = wf.build_graph(saver)
    final = await graph2.ainvoke(Command(resume={"approved": True}), config=cfg)

    assert final["status"] == "PURCHASE_CREATED"
