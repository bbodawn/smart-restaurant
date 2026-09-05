from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field
from app.core.llm import get_llm, get_structured_llm
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


def inventory_analysis_node(state: PurchaseState) -> dict:
    """库存事实分析节点（Phase 3-A，确定性、无 LLM/DB/interrupt）。

    只产出库存 Facts 写入 state["analysis_inventory"]，不做采购决策
    （是否采购 / 采购数量等归后续 deterministic_policy 阶段）。
    """
    current_stock = float(state.get("current_stock", 0) or 0)
    daily_sales = float(state.get("daily_sales", 0) or 0)
    safety_stock = float(state.get("safety_stock", 0) or 0)

    predicted_3_day_demand = daily_sales * 3

    # 信息指标：当前库存可覆盖天数；无消耗（daily_sales<=0）时不存在有意义的覆盖天数。
    # 仅作展示/参考，不构成任何“3~7 天”采购规则。
    if daily_sales > 0:
        stock_coverage_days = round(current_stock / daily_sales, 2)
    else:
        stock_coverage_days = None

    # 仅描述库存所处状态（事实），不触发任何采购/挂起动作。
    if current_stock < safety_stock:
        health_status = "BELOW_SAFETY"
    elif current_stock < predicted_3_day_demand:
        health_status = "LOW_STOCK"
    else:
        health_status = "HEALTHY"

    return {
        "analysis_inventory": {
            "current_stock": round(current_stock, 2),
            "daily_sales": round(daily_sales, 2),
            "safety_stock": round(safety_stock, 2),
            "predicted_3_day_demand": round(predicted_3_day_demand, 2),
            "stock_coverage_days": stock_coverage_days,
            "health_status": health_status,
        }
    }


def price_analysis_node(state: PurchaseState) -> dict:
    """价格事实分析节点（Phase 3-A，确定性、无 LLM/DB/interrupt）。

    只计算价格偏离 Facts 写入 state["analysis_price"]；
    是否触发 HITL / 是否采购由后续阶段决定，本节点不做决策。
    """
    supplier_price = float(state.get("supplier_price", 0) or 0)
    historical_price = float(state.get("historical_price", 0) or 0)

    # 沿用现有 risk_agent 的偏离口径，并对 historical_price <= 0 安全兜底（不产生 ZeroDivisionError）。
    if historical_price > 0:
        price_deviation = abs(supplier_price - historical_price) / historical_price
        # 20% 为现有系统的参考观察线，此处仅作状态描述，不触发任何审批动作
        price_status = "WITHIN_20PCT" if price_deviation <= 0.20 else "GT_20PCT"
    else:
        price_deviation = 0.0
        price_status = "NO_HISTORICAL_PRICE"

    return {
        "analysis_price": {
            "supplier_price": round(supplier_price, 2),
            "historical_price": round(historical_price, 2),
            "price_deviation": round(price_deviation, 4),
            "price_status": price_status,
        }
    }


def supplier_analysis_node(state: PurchaseState) -> dict:
    """供应商事实快照节点（Phase 3-A，确定性、无 LLM/DB/interrupt）。

    仅输出当前单据可确认的供应商字段写入 state["analysis_supplier"]；
    不实现多供应商选择 / 排序 / 最优供应商 / 比价（当前数据面不支持，不虚构业务能力）。
    """
    supplier_id = state.get("supplier_id")
    supplier_name = state.get("supplier_name")
    supplier_price = float(state.get("supplier_price", 0) or 0)

    return {
        "analysis_supplier": {
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "supplier_price": round(supplier_price, 2),
        }
    }


def _policy_num(value, default: float = 0.0) -> float:
    """确定性 Policy 输入安全转 float；None/非法值回落 default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def deterministic_policy_node(state: PurchaseState) -> dict:
    """确定性业务规则层（Phase 3-A 第二小步，无 LLM/DB/HITL/interrupt）。

    输入优先取 analysis_inventory / analysis_price / analysis_supplier 三个
    分析层 Facts；缺失时回落到 state 顶层既有字段。仅返回决策结果写入
    state["policy_decision"]：不审批、不挂起、不调用 Agent、不建单、不写库。
    """
    inv = state.get("analysis_inventory") or {}
    prc = state.get("analysis_price") or {}
    sup = state.get("analysis_supplier") or {}

    current_stock = _policy_num(inv.get("current_stock", state.get("current_stock")))
    daily_sales = _policy_num(inv.get("daily_sales", state.get("daily_sales")))
    safety_stock = _policy_num(inv.get("safety_stock", state.get("safety_stock")))
    predicted_3_day_demand = _policy_num(
        inv.get("predicted_3_day_demand", daily_sales * 3)
    )
    supplier_price = _policy_num(
        sup.get("supplier_price", prc.get("supplier_price", state.get("supplier_price")))
    )
    historical_price = _policy_num(prc.get("historical_price", state.get("historical_price")))

    # 价格偏离：优先复用分析层已有结果；否则按现有口径计算，
    # historical_price <= 0 时安全兜底为 0.0（不产生 ZeroDivisionError）。
    if prc.get("price_deviation") is not None:
        price_deviation = _policy_num(prc.get("price_deviation"))
    elif historical_price > 0:
        price_deviation = abs(supplier_price - historical_price) / historical_price
    else:
        price_deviation = 0.0

    # 【采购判断】沿用现有 3 日预测需求口径（predicted_3_day_demand = daily_sales * 3）
    if current_stock >= predicted_3_day_demand and current_stock > safety_stock:
        needs_purchase = False
        quantity = 0.0
    else:
        needs_purchase = True
        quantity = max(predicted_3_day_demand - current_stock, 0.0)

    # 【订单金额】沿用现有两位小数口径
    total_amount = round(quantity * supplier_price, 2)

    # 【风险判定】仅确定性规则；沿用现有标签与“<=”库存口径，不触发任何审批动作
    risk_flags = []
    if price_deviation > 0.20:
        risk_flags.append("PRICE_DEVIATION_GT_20%")
    if current_stock <= safety_stock:
        risk_flags.append("LOW_STOCK_BELOW_SAFETY")

    # 【最终 Policy 状态】NO_PURCHASE / REVIEW / PURCHASE
    if not needs_purchase:
        status = "NO_PURCHASE"
    elif risk_flags:
        status = "REVIEW"
    else:
        status = "PURCHASE"

    risk_reason = ",".join(risk_flags) if risk_flags else None

    return {
        "policy_decision": {
            "status": status,
            "needs_purchase": needs_purchase,
            "quantity": round(quantity, 2),
            "total_amount": total_amount,
            "risk_flags": risk_flags,
            "risk_reason": risk_reason,
        }
    }


class Agent5RiskAnalysis(BaseModel):
    """Agent 5 结构化输出：风险综合分析与业务解释。

    仅生成面向人工审核的自然语言分析/建议文本；不含任何决策/状态字段
    （如 status / approved / quantity），禁止改变 deterministic_policy 的结论。
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="结合库存/价格/供应商与分析结论的一句话概况（中文）")
    risk_level: str = Field(description="综合风险等级，如 HIGH / MEDIUM / LOW")
    risk_analysis: str = Field(description="解释当前触发 REVIEW 的风险及业务影响（中文，不得虚构数据）")
    recommendation: str = Field(description="给人工审核的建议文本（中文，仅是建议，不是路由/审批命令）")


async def agent5_node(state: PurchaseState) -> dict:
    """风险综合分析 / 业务解释 Agent（Phase 3-B Step 2，Agent 5）。

    职责范围（仅在 REVIEW 场景执行）：
    - 只读分析 + 非确定性业务解释，生成面向人工审核的自然语言风险报告写入
      state["agent5_analysis"]（summary / risk_level / risk_analysis / recommendation）。
    - 只基于 State 已有数据（analysis_* / policy_decision / 基础字段），不自查 DB、不改 DB。

    严格权限边界：
    - 绝不修改 / 覆盖 state["policy_decision"]（Policy 是确定性裁判）。
    - 绝不修改 quantity / total_amount，不允许自行批准采购、计算采购数量/金额。
    - recommendation 仅是建议文本，不是 Graph 路由命令，也不是审批结果。

    安全失败：仅输出字段缺失、非 REVIEW 防御、LLM 不可用/解析失败等一律安全失败——
    不伪造分析、不改判 REVIEW、不自动升级为 PURCHASE、不绕过风险审核、不执行采购。
    """
    inv = state.get("analysis_inventory") or {}
    prc = state.get("analysis_price") or {}
    sup = state.get("analysis_supplier") or {}
    policy = state.get("policy_decision") or {}

    fallback = {
        "summary": "本次采购被确定为需人工审核的高风险订单；当前未生成完整智能风险分析，请结合下方采购数据进行人工审核。",
        "risk_level": "UNKNOWN",
        "risk_analysis": "暂无法自动生成详细风险文本（大模型分析不可用或输入不完整）。系统已按确定性 Policy 保留 REVIEW 判定，未改变采购数量/状态，也未自动批准或执行采购。",
        "recommendation": "请结合下方库存、报价与偏离数据人工审核后再决定是否采购。",
    }

    # 输入完整性/契约防御：仅当确定为 REVIEW 且有 Policy 依据时才调用 LLM
    if policy.get("status") != "REVIEW":
        return {"agent5_analysis": fallback}

    inv_risk = inv.get("stock_coverage_days")
    if inv_risk is not None:
        inv_risk = float(inv_risk)
    inv_text = (
        f"库存当前库存 {inv.get('current_stock')}{state.get('unit', '')}，"
        f"安全库存 {inv.get('safety_stock')}{state.get('unit', '')}，"
        f"可覆盖约 {inv_risk} 天的 3 日预测需求（health_status={inv.get('health_status')}）。"
        if inv else f"当前库存 {state.get('current_stock')}{state.get('unit', '')}，"
        f"安全库存 {state.get('safety_stock')}{state.get('unit', '')}。"
    )
    supplier_text = (
        f"供应商「{sup.get('supplier_name') or state.get('supplier_name')}」"
        f"当前报价 {sup.get('supplier_price') or state.get('supplier_price')} 元/{state.get('unit', 'kg')}。"
    )
    policy_text = (
        f"确定性 Policy 判定为 {policy.get('status')}，触发项：{policy.get('risk_flags', [])}，"
        f"建议采购数量 {policy.get('quantity')}{state.get('unit', '')}，采购金额按 Policy 口径计算。"
    )

    prompt = (
        "你是餐饮供应链的风险综合分析助手，不是采购执行者，也不是最终裁判。\n"
        "必须遵守以下权限边界：\n"
        "1. deterministic_policy 的判定（status=REVIEW）是确定性规则的最终依据，你只能解释当前风险。\n"
        "2. 绝对不允许修改或覆盖 policy 的结论，不允许把 REVIEW 改成 PURCHASE/APPROVED。\n"
        "3. 不允许自行批准采购、不允许自己计算采购数量或采购金额。\n"
        "4. 只允许基于以下给出的 State 数据作答，禁止虚构数据库中不存在的信息。\n"
        "5. recommendation 只能给建议文本，不能是路由命令或审批结论。\n"
        "请基于以下事实输出结构化风险分析：\n"
        f"- 食材：{state.get('ingredient')}（{state.get('unit', 'kg')}）\n"
        f"- {inv_text}\n"
        f"- 供应商报价：{prc.get('supplier_price') or state.get('supplier_price')} 元/{state.get('unit', 'kg')}，"
        f"历史均价 {prc.get('historical_price') or state.get('historical_price')} 元/{state.get('unit', 'kg')}，"
        f"偏离 {prc.get('price_deviation')}。（price_status={prc.get('price_status')}）\n"
        f"- {supplier_text}\n"
        f"- {policy_text}\n"
        "用中文输出 summary / risk_level / risk_analysis / recommendation 四个字段。\n"
        "注意：不要只罗列风险项或翻译枚举；要结合上面给出的库存、3 天需求、安全线、报价与偏离等数值，"
        "解释这些风险对供应或成本的实际业务影响，并给审查人一句可操作的采购建议（尽量简洁、基于给定数据、不虚构）。"
    )

    try:
        llm = get_structured_llm(Agent5RiskAnalysis)
        parsed = await llm.ainvoke(prompt)
        result = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    except Exception:
        result = fallback

    # 最终防线：无论 LLM 输出如何，本节点返回值只声明 agent5_analysis 这一只写键，
    # langgraph 字典合并不改动 policy_decision / quantity / total_amount / status。
    return {"agent5_analysis": result}


async def purchase_approval_node(state: PurchaseState) -> Command:
    """Human-in-the-Loop 审批节点（Phase 3-B Step 3-A）。

    只出现在 REVIEW 分支（agent5 -> purchase_approval）。Agent 5 之后、采购执行之前
    由人工来决定。职责仅限：读取当前 State -> 组装给审批 UI/API 的 payload -> 调用
    interrupt() 挂起等待人工 -> 依据 resume 的人工 approved 决定 Graph 后续路径。

    权限边界：
    - approved 只能来自 Human（resume 注入），本节点/Agent5 无权代批、无权自动批准。
    - 不修改 policy_decision / quantity / total_amount（Policy 是确定性裁判）。
    - approve(True)  -> Command(goto="purchase_prepare")，由既有 purchase_prepare/
      purchase_agent 按其它逻辑收口；不直接执行采购/入库。
    - reject(False)  -> Command(goto=END)，绝不进入 purchase_agent / purchase_prepare。
    - interrupt/resume 异常一律不上浮为批准：只需安全校验，非合法批准视为拒绝，不自动采购。
    """
    policy = state.get("policy_decision") or {}
    inv = state.get("analysis_inventory") or {}
    prc = state.get("analysis_price") or {}
    policy_qty = policy.get("quantity", 0)

    # 组装给人工审批界面的快照 payload（仅信息，不含可执行命令）
    payload = {
        "type": "PURCHASE_REVIEW_APPROVAL",
        "order_id": state.get("order_id"),
        "order_no": state.get("order_no"),
        "ingredient": state.get("ingredient"),
        "unit": state.get("unit", "kg"),
        "current_stock": inv.get("current_stock") if inv else state.get("current_stock"),
        "safety_stock": inv.get("safety_stock") if inv else state.get("safety_stock"),
        "supplier_name": (state.get("analysis_supplier") or {}).get("supplier_name")
        if state.get("analysis_supplier") else state.get("supplier_name"),
        "supplier_price": prc.get("supplier_price") if prc else state.get("supplier_price"),
        "historical_price": prc.get("historical_price") if prc else state.get("historical_price"),
        "price_deviation": prc.get("price_deviation", 0.0),
        "risk_flags": policy.get("risk_flags", []),
        "risk_reason": policy.get("risk_reason"),
        "quantity": policy_qty,
        "total_amount": policy.get("total_amount", 0.0),
        "agent5_analysis": state.get("agent5_analysis"),
    }

    # 挂起等待人工：interrupt 会在本节点抛 GraphInterrupt 使 langgraph 真正暂停。
    # 注意：不要用 try/except 包住 interrupt —— 那样会吞掉 GraphInterrupt，
    # 导致 HITL 永远不真正挂起（会把中断误当普通异常吞掉后直接 goto END）。
    decision = interrupt(payload)

    approved = False
    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
    else:
        approved = bool(decision)
    # 非法/缺失的人工输入一律按未批准（reject）处理，绝不擅自代批，也不自动采购。

    if approved:
        # 人工批准 -> 进入既有采购收口（purchase_prepare 会把顶层 status 置 APPROVED，
        # purchase_agent 据此推进 PURCHASE_CREATED）；HITL 不做真实采购/入库。
        return Command(goto="purchase_prepare", update={})
    # 人工拒绝 / 非法输入 —— 一律落到 END，绝不进入 purchase_agent/purchase_prepare
    return Command(goto=END, update={})
