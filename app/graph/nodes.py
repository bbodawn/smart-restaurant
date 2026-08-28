from langgraph.types import interrupt
from app.graph.state import PurchaseState

async def demand_agent(state: PurchaseState) -> dict:
    daily_sales = float(state["daily_sales"])
    predicted_demand = daily_sales * 3
    return {"predicted_demand": predicted_demand}

async def inventory_agent(state: PurchaseState) -> dict:
    current_stock = float(state["current_stock"])
    predicted_demand = float(state["predicted_demand"])

    if current_stock >= predicted_demand:
        return {"quantity": 0.0, "status": "NO_PURCHASE"}

    quantity = predicted_demand - current_stock
    return {"quantity": quantity, "status": "NEED_PURCHASE"}

async def supplier_agent(state: PurchaseState) -> dict:
    quantity = float(state["quantity"])
    supplier_price = float(state["supplier_price"])
    total_amount = quantity * supplier_price
    return {"total_amount": round(total_amount, 2)}

async def risk_agent(state: PurchaseState) -> dict:
    if state["status"] == "NO_PURCHASE":
        return {"approved": True, "status": "NO_PURCHASE"}

    supplier_price = float(state["supplier_price"])
    historical_price = float(state["historical_price"])
    total_amount = float(state["total_amount"])

    if historical_price == 0:
        price_deviation = 0.0
    else:
        price_deviation = abs(supplier_price - historical_price) / historical_price

    reasons = []
    if price_deviation > 0.20:
        reasons.append("PRICE_DEVIATION_GT_20%")
    if total_amount > 5000:
        reasons.append("ORDER_AMOUNT_GT_5000")

    if not reasons:
        return {
            "price_deviation": price_deviation,
            "risk_reason": None,
            "approved": True,
            "status": "APPROVED",
        }

    decision = interrupt({
        "type": "PURCHASE_APPROVAL",
        "order_id": state["order_id"],
        "order_no": state["order_no"],
        "ingredient": state["ingredient"],
        "quantity": state["quantity"],
        "supplier_name": state["supplier_name"],
        "supplier_price": supplier_price,
        "historical_price": historical_price,
        "price_deviation": round(price_deviation, 4),
        "total_amount": total_amount,
        "risk_reason": reasons,
    })

    approved = False
    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
    else:
        approved = bool(decision)

    if approved:
        return {
            "price_deviation": price_deviation,
            "risk_reason": ",".join(reasons),
            "approved": True,
            "status": "APPROVED",
        }

    return {
        "price_deviation": price_deviation,
        "risk_reason": ",".join(reasons),
        "approved": False,
        "status": "REJECTED",
    }

async def purchase_agent(state: PurchaseState) -> dict:
    status = state["status"]
    if status == "NO_PURCHASE":
        return {"status": "NO_PURCHASE"}
    if status == "REJECTED":
        return {"status": "REJECTED"}
    if status == "APPROVED":
        return {"status": "PURCHASE_CREATED"}
    return {"status": status}
