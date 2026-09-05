"""Deterministic Policy 核心业务契约（纯同步、无任何基础设施依赖）。

目标：把「确定性规则层」的输出钉死为不可回归的契约。
覆盖：NO_PURCHASE / PURCHASE / REVIEW；库存风险；价格>20% 风险；双风险；
采购数量；total_amount；金额>5000 规则【不存在】的防回归；<= 边界；兜底安全。
"""
import pytest

from app.graph.nodes import (
    deterministic_policy_node,
    inventory_analysis_node,
    price_analysis_node,
    supplier_analysis_node,
)

# policy_decision 的六字段契约：下游 Service/API 依赖这套结构
POLICY_KEYS = {
    "status",
    "needs_purchase",
    "quantity",
    "total_amount",
    "risk_flags",
    "risk_reason",
}


def _analysis_state(current, daily, safety, price, hist):
    """仿真真实图路径：先用三个分析节点算事实，再喂给 policy。

    这样 policy 测试消费的 analysis_* 与线上完全一致，避免手抄公式偏移。
    """
    state = {
        "ingredient": "测试食材",
        "unit": "kg",
        "current_stock": current,
        "daily_sales": daily,
        "safety_stock": safety,
        "supplier_id": 1,
        "supplier_name": "测试供应商",
        "supplier_price": price,
        "historical_price": hist,
    }
    state.update(inventory_analysis_node(state))
    state.update(price_analysis_node(state))
    state.update(supplier_analysis_node(state))
    return state


def _fallback_state(current, daily, safety, price, hist):
    """不走分析层，直接给顶层字段（policy 的兜底读取路径）。"""
    return {
        "ingredient": "测试食材",
        "unit": "kg",
        "current_stock": current,
        "daily_sales": daily,
        "safety_stock": safety,
        "supplier_id": 1,
        "supplier_name": "测试供应商",
        "supplier_price": price,
        "historical_price": hist,
    }


def _decide(state):
    return deterministic_policy_node(state)["policy_decision"]


@pytest.mark.parametrize("builder", [_analysis_state, _fallback_state], ids=["via-analysis", "via-fallback"])
def test_no_purchase_when_stock_healthy(builder):
    """库存充足：>=3日预测需求 且 >安全线 → NO_PURCHASE，数量 0。"""
    d = _decide(builder(50, 10, 12, 5, 5))
    assert d["status"] == "NO_PURCHASE"
    assert d["needs_purchase"] is False
    assert d["quantity"] == 0.0
    assert d["total_amount"] == 0.0
    assert d["risk_flags"] == []
    assert d["risk_reason"] is None
    assert set(d) == POLICY_KEYS


@pytest.mark.parametrize("builder", [_analysis_state, _fallback_state], ids=["via-analysis", "via-fallback"])
def test_purchase_when_below_3day_demand_and_no_risk(builder):
    """低于3日预测需求、未跌破安全线、价格正常 → PURCHASE，数量=max(预测-库存,0)。"""
    d = _decide(builder(20, 10, 12, 5, 5))  # pred=30, 缺口 10
    assert d["status"] == "PURCHASE"
    assert d["needs_purchase"] is True
    assert d["quantity"] == 10.0
    assert d["total_amount"] == 50.0          # 10 × 5
    assert d["risk_flags"] == []
    assert d["risk_reason"] is None


@pytest.mark.parametrize("builder", [_analysis_state, _fallback_state], ids=["via-analysis", "via-fallback"])
def test_review_when_stock_below_safety(builder):
    """库存 ≤ 安全线（触及/跌破）→ 触发库存风险 → REVIEW。"""
    d = _decide(builder(10, 10, 12, 5, 5))  # cur<=safety
    assert d["status"] == "REVIEW"
    assert d["needs_purchase"] is True
    assert "LOW_STOCK_BELOW_SAFETY" in d["risk_flags"]
    assert d["risk_reason"] == "LOW_STOCK_BELOW_SAFETY"
    assert d["quantity"] == 20.0
    assert d["total_amount"] == 100.0


def test_review_when_price_deviation_gt_20pct_and_needs_purchase():
    """价格偏离>20% 且确实需采购 → REVIEW（价格风险）。"""
    d = _decide(_analysis_state(20, 10, 12, 30, 20))  # pred30, dev=0.5
    assert d["status"] == "REVIEW"
    assert "PRICE_DEVIATION_GT_20%" in d["risk_flags"]
    assert d["quantity"] == 10.0
    assert d["total_amount"] == 300.0


def test_price_risk_is_flagged_even_when_no_purchase_needed():
    """契约钉死：库存健康但价格偏离>20% → 状态仍是 NO_PURCHASE（不买则不受价格风险牵制），
    但 risk_flags 仍记录价格异常作为审计信息。"""
    d = _decide(_analysis_state(50, 10, 12, 30, 20))  # 库存健康 pred30
    assert d["status"] == "NO_PURCHASE"
    assert "PRICE_DEVIATION_GT_20%" in d["risk_flags"]  # 仅审计，不改状态


def test_review_on_both_low_stock_and_price_risk():
    """库存 + 价格双风险：两条 flag 都出现，状态 REVIEW。"""
    d = _decide(_analysis_state(10, 10, 12, 30, 20))
    assert d["status"] == "REVIEW"
    assert set(d["risk_flags"]) == {"LOW_STOCK_BELOW_SAFETY", "PRICE_DEVIATION_GT_20%"}
    assert "LOW_STOCK_BELOW_SAFETY" in d["risk_reason"]
    assert "PRICE_DEVIATION_GT_20%" in d["risk_reason"]
    assert d["quantity"] == 20.0


def test_quantity_and_total_rounding():
    """采购数量与金额的小数精度契约。"""
    d = _decide(_analysis_state(12.5, 7.5, 10, 6.4, 6.0))  # pred=22.5, 缺口=10.0
    assert d["quantity"] == 10.0
    assert d["total_amount"] == 64.0


def test_no_amount_gt_5000_rule_exists():
    """防回归：金额 >5000 的旧风险规则在 Policy 中【已移除】——
    大额订单若无库存/价格风险，仍应为 PURCHASE 而非 REVIEW。
    这是与旧 risk_agent 的行为差异，视为有意设计。"""
    d = _decide(_analysis_state(100, 600, 30, 20, 18))  # pred=1800, qty=1700, total=34000
    assert d["status"] == "PURCHASE"
    assert d["risk_flags"] == []          # 无金额风险
    assert d["quantity"] == 1700.0
    assert d["total_amount"] == 34000.0


def test_no_division_by_zero_when_historical_price_zero():
    """historical_price<=0：偏离安全回退 0，不抛 ZeroDivisionError，不误报价格风险。"""
    d = _decide(_analysis_state(20, 10, 12, 30, 0))
    assert d["status"] == "PURCHASE"
    assert "PRICE_DEVIATION_GT_20%" not in d["risk_flags"]
    assert d["total_amount"] == 300.0


def test_low_stock_boundary_uses_less_equal():
    """边界契约：current_stock 恰等于 safety_stock 视为跌破（<=）→ LOW_STOCK。
    （即便库存同时满足预测需求，因不满足 `>安全线`，仍判定需采购并 REVIEW。）"""
    d = _decide(_analysis_state(30, 10, 30, 5, 5))  # cur==pred==safety
    assert d["status"] == "REVIEW"
    assert "LOW_STOCK_BELOW_SAFETY" in d["risk_flags"]
    assert d["quantity"] == 0.0
