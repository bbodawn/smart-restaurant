from langgraph.types import interrupt
from app.core.llm import get_llm
from app.graph.state import PurchaseState

async def demand_agent(state: PurchaseState) -> dict:
    daily_sales = float(state["daily_sales"])
    predicted_demand = daily_sales * 3

    demand_reasoning = (
        f"根据近30天销量统计，日均消耗 {daily_sales}kg，"
        f"按3天预测周期，预测需求为 {predicted_demand}kg。"
    )

    try:
        llm = get_llm()
        prompt = (
            f"你是餐饮后厨的采购需求分析师。当前食材为「{state['ingredient']}」，"
            f"日均销量 {daily_sales}kg，预测天数 3 天，故基础预测需求为 {predicted_demand}kg，"
            f"当前库存 {state['current_stock']}kg。"
            f"请用一句简洁的中文总结本次需求分析与预测依据。"
        )
        response = await llm.ainvoke(prompt)
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            demand_reasoning = content.strip()
    except Exception:
        pass

    return {"predicted_demand": predicted_demand, "demand_reasoning": demand_reasoning}

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
            "risk_analysis_report": None,
            "approved": True,
            "status": "APPROVED",
        }

    risk_analysis_report = (
        f"供应商「{state['supplier_name']}」报价 {supplier_price} 元/kg，"
        f"历史均价 {historical_price} 元/kg，偏离 {price_deviation * 100:.1f}%，"
        f"预测采购总额 {total_amount} 元，触发人工审批。"
    )

    try:
        llm = get_llm()
        reason_hint = "、".join(reasons)
        prompt = (
            f"你是餐饮供应链风控分析师。请针对以下采购风险生成一段简明清晰的中文风控告警报告：\n"
            f"- 食材：{state['ingredient']}\n"
            f"- 采购数量：{state['quantity']}kg\n"
            f"- 供应商：{state['supplier_name']}\n"
            f"- 当前报价：{supplier_price} 元/kg\n"
            f"- 历史均价：{historical_price} 元/kg\n"
            f"- 价格偏离度：{price_deviation * 100:.1f}%\n"
            f"- 预测采购总额：{total_amount} 元\n"
            f"- 触发风险项：{reason_hint}\n"
            f"请明确指出风险点并给出是否建议放行的判断依据（100字以内）。"
        )
        response = await llm.ainvoke(prompt)
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            risk_analysis_report = content.strip()
    except Exception:
        pass

    interrupt_value = {
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
        "risk_analysis_report": risk_analysis_report,
    }

    decision = interrupt(interrupt_value)

    approved = False
    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
    else:
        approved = bool(decision)

    if approved:
        return {
            "price_deviation": price_deviation,
            "risk_reason": ",".join(reasons),
            "risk_analysis_report": risk_analysis_report,
            "approved": True,
            "status": "APPROVED",
        }

    return {
        "price_deviation": price_deviation,
        "risk_reason": ",".join(reasons),
        "risk_analysis_report": risk_analysis_report,
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
