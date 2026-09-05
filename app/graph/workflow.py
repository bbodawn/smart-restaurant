from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    agent5_node,
    deterministic_policy_node,
    inventory_analysis_node,
    price_analysis_node,
    purchase_agent,
    purchase_approval_node,
    supplier_analysis_node,
)
from app.graph.state import PurchaseState

def route_after_policy(state) -> str:
    """确定性条件路由（Phase 3-B / Step 1）：仅依据 policy_decision.status 路由。

    不调用 LLM、不访问 DB、不修改 State、不执行采购：
    - NO_PURCHASE -> "no_purchase"（结束）
    - PURCHASE    -> "purchase"（走 purchase_prepare -> purchase_agent）
    - REVIEW      -> "review"（走 Agent 5 风险综合分析；HITL 属后续 Step）
    未知状态直接抛错，避免静默走向错误分支。
    """
    status = state["policy_decision"]["status"]
    if status == "NO_PURCHASE":
        return "no_purchase"
    if status == "PURCHASE":
        return "purchase"
    if status == "REVIEW":
        return "review"
    raise ValueError(f"Unknown policy status: {status}")

def purchase_prepare(state) -> dict:
    """确定性输入契约适配节点（Phase 3-B / Step 1）。

    仅做新链路→旧 purchase_agent 的输入契约适配：把顶层 status 置为
    "APPROVED"，令旧 purchase_agent 按其原逻辑把状态推进到 PURCHASE_CREATED。
    非 Agent、不做决策、不访问 DB、不调用 LLM、不执行采购、不触碰业务规则。
    """
    return {"status": "APPROVED"}

def build_graph(checkpointer: AsyncRedisSaver):
    builder = StateGraph(PurchaseState)

    # Phase 3-A 只读分析层：三个确定性 Facts 节点，由 START 并行 Fan-out
    builder.add_node("inventory_analysis", inventory_analysis_node)
    builder.add_node("price_analysis", price_analysis_node)
    builder.add_node("supplier_analysis", supplier_analysis_node)
    # Phase 3-A 确定性业务规则层：三个分析节点全部完成后 Fan-in 执行
    builder.add_node("deterministic_policy", deterministic_policy_node)
    # PURCHASE 分支复用既有采购落单节点（业务逻辑本轮不动，未接入并行的三个上游分析）
    builder.add_node("purchase_prepare", purchase_prepare)
    builder.add_node("purchase_agent", purchase_agent)
    # REVIEW 分支走 Agent 5（风险综合分析/业务解释），随后进入 HITL 审批节点
    builder.add_node("agent5", agent5_node)
    builder.add_node("purchase_approval", purchase_approval_node)

    builder.add_edge(START, "inventory_analysis")
    builder.add_edge(START, "price_analysis")
    builder.add_edge(START, "supplier_analysis")

    builder.add_edge("inventory_analysis", "deterministic_policy")
    builder.add_edge("price_analysis", "deterministic_policy")
    builder.add_edge("supplier_analysis", "deterministic_policy")

    # 条件路由：NO_PURCHASE→END；REVIEW→Agent5→END；PURCHASE 经 purchase_prepare 适配后走既有 purchase_agent
    builder.add_conditional_edges(
        "deterministic_policy",
        route_after_policy,
        {
            "no_purchase": END,
            "purchase": "purchase_prepare",
            "review": "agent5",
        },
    )
    builder.add_edge("agent5", "purchase_approval")
    builder.add_edge("purchase_prepare", "purchase_agent")
    builder.add_edge("purchase_agent", END)

    return builder.compile(checkpointer=checkpointer)
