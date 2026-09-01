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
    safety_stock = float(state.get("safety_stock", 0) or 0)

    # 只有库存既能覆盖短期预测需求、又高于安全线时，才判定无需采购。
    # 否则（尤其当库存已低于安全线）必须进入采购 → RiskAgent 低库存挂起。
    if current_stock >= predicted_demand and current_stock > safety_stock:
        return {"quantity": 0.0, "status": "NO_PURCHASE"}

    # 补货量至少覆盖缺口补回安全线，避免只覆盖短期需求导致低频反复触发
    quantity = predicted_demand - current_stock
    if safety_stock > 0 and safety_stock - current_stock > quantity:
        quantity = safety_stock - current_stock
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
    current_stock = float(state.get("current_stock", 0))
    safety_stock = float(state.get("safety_stock", 0))

    if historical_price == 0:
        price_deviation = 0.0
    else:
        price_deviation = abs(supplier_price - historical_price) / historical_price

    reasons = []
    if price_deviation > 0.20:
        reasons.append("PRICE_DEVIATION_GT_20%")
    if total_amount > 5000:
        reasons.append("ORDER_AMOUNT_GT_5000")
    # 库存风控（新增）：当前库存低于或触及安全基线时自动挂起，防止急单履约风险
    if safety_stock > 0 and current_stock <= safety_stock:
        reasons.append("LOW_STOCK_BELOW_SAFETY")

    if not reasons:
        return {
            "price_deviation": price_deviation,
            "risk_reason": None,
            "risk_analysis_report": None,
            "approved": True,
            "status": "APPROVED",
        }

    # 确定性的结构化告警（保证低库存/价格风险格式稳定出现在报告中）
    report_parts = []
    if "LOW_STOCK_BELOW_SAFETY" in reasons:
        drop_pct = 0.0
        if safety_stock > 0:
            drop_pct = max(0.0, (safety_stock - current_stock) / safety_stock * 100)
        report_parts.append(
            f"[低库存风险告警] 当前食材库存 ({current_stock}kg) 已低于安全基线 ({safety_stock}kg)，"
            f"降幅达 {drop_pct:.0f}%。为防止急单履约风险，系统自动挂起，建议人工核对供应商到货时效与采购数量。"
        )
    if "PRICE_DEVIATION_GT_20%" in reasons:
        report_parts.append(
            f"[价格风险告警] 供应商「{state['supplier_name']}」报价 {supplier_price} 元/kg，"
            f"历史均价 {historical_price} 元/kg，偏离 {price_deviation * 100:.1f}%，预测采购总额 {total_amount} 元。"
        )
    if "ORDER_AMOUNT_GT_5000" in reasons:
        report_parts.append(f"[金额风险告警] 预测采购总额 {total_amount} 元，超过 5000 元阈值。")
    risk_analysis_report = "\n".join(report_parts)

    try:
        llm = get_llm()
        reason_hint = "、".join(reasons)
        low_stock_note = (
            f"当前库存 {current_stock}kg 已低于安全基线 {safety_stock}kg（降幅 "
            f"{(max(0.0, (safety_stock - current_stock) / safety_stock * 100) if safety_stock > 0 else 0.0):.0f}%）。"
            if "LOW_STOCK_BELOW_SAFETY" in reasons else f"当前库存 {current_stock}kg，安全基线 {safety_stock}kg。"
        )
        prompt = (
            f"你是餐饮供应链风控分析师。请针对以下采购风险补充一段简明清晰的中文分析（不重复标签）：\n"
            f"- 食材：{state['ingredient']}\n"
            f"- 采购数量：{state['quantity']}kg\n"
            f"- 库存：{low_stock_note}\n"
            f"- 供应商：{state['supplier_name']}\n"
            f"- 当前报价：{supplier_price} 元/kg\n"
            f"- 历史均价：{historical_price} 元/kg\n"
            f"- 价格偏离度：{price_deviation * 100:.1f}%\n"
            f"- 预测采购总额：{total_amount} 元\n"
            f"- 触发风险项：{reason_hint}\n"
            f"请明确指出风险点并给出是否建议放行的判断依据（80字以内）。"
        )
        response = await llm.ainvoke(prompt)
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            risk_analysis_report = risk_analysis_report + "\n—— RiskAgent 补充分析 ——\n" + content.strip()
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


def extension_agent_node(state: PurchaseState) -> dict:
    """扩展决策 Agent (ExtensionAgent) 占位节点。

    为后续新增的自定义策略 Agent 预留：
    - 当前仅写入标记文本，按默认规则放行，不改变任何采购决策。
    - 该节点未接入主流程（未 add_edge），因此不影响现有 5-Agent 链路。
    """
    return {
        "extension_agent_analysis": "【扩展 Agent】当前策略：按默认规则放行（等待新功能定义）",
    }
