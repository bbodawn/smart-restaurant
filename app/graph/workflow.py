from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import (
    demand_agent,
    inventory_agent,
    supplier_agent,
    risk_agent,
    purchase_agent,
)
from app.graph.state import PurchaseState

def build_graph(checkpointer: AsyncRedisSaver):
    builder = StateGraph(PurchaseState)

    builder.add_node("demand_agent", demand_agent)
    builder.add_node("inventory_agent", inventory_agent)
    builder.add_node("supplier_agent", supplier_agent)
    builder.add_node("risk_agent", risk_agent)
    builder.add_node("purchase_agent", purchase_agent)

    builder.add_edge(START, "demand_agent")
    builder.add_edge("demand_agent", "inventory_agent")
    builder.add_edge("inventory_agent", "supplier_agent")
    builder.add_edge("supplier_agent", "risk_agent")
    builder.add_edge("risk_agent", "purchase_agent")
    builder.add_edge("purchase_agent", END)

    return builder.compile(checkpointer=checkpointer)
