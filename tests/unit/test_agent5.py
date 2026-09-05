"""Agent5（LLM 风险解释）核心契约。

覆盖：结构化 schema；LLM 成功输出四字段；LLM 失败 fallback；
非 REVIEW 不调 LLM；无论如何【不得修改 policy_decision / quantity / status】。

所有用例 monkeypatch 掉真实 LLM —— 本模块不连 Ollama，离线可跑。
"""
import pytest
from pydantic import ValidationError

from app.graph.nodes import Agent5RiskAnalysis, agent5_node

# 一份真实 REVIEW 图路径会产生的输入快照
REVIEW_STATE = {
    "ingredient": "优质猪肉",
    "unit": "kg",
    "current_stock": 10.0,
    "safety_stock": 12.0,
    "daily_sales": 10.0,
    "supplier_price": 30.0,
    "historical_price": 20.0,
    "analysis_inventory": {
        "current_stock": 10.0,
        "daily_sales": 10.0,
        "safety_stock": 12.0,
        "predicted_3_day_demand": 30.0,
        "stock_coverage_days": 1.0,
        "health_status": "LOW_STOCK",
    },
    "analysis_price": {
        "supplier_price": 30.0,
        "historical_price": 20.0,
        "price_deviation": 0.5,
        "price_status": "GT_20PCT",
    },
    "analysis_supplier": {"supplier_id": 3, "supplier_name": "华东肉类供应商", "supplier_price": 30.0},
    "policy_decision": {
        "status": "REVIEW",
        "needs_purchase": True,
        "quantity": 20.0,
        "total_amount": 600.0,
        "risk_flags": ["PRICE_DEVIATION_GT_20%", "LOW_STOCK_BELOW_SAFETY"],
        "risk_reason": "PRICE_DEVIATION_GT_20%,LOW_STOCK_BELOW_SAFETY",
    },
}

FALLBACK_KEYS = {"summary", "risk_level", "risk_analysis", "recommendation"}


def _copy_review(**over):
    import copy
    return {**copy.deepcopy(REVIEW_STATE), **over}


# ---------- schema ----------

def test_agent5_schema_has_exact_four_fields():
    m = Agent5RiskAnalysis(summary="s", risk_level="HIGH", risk_analysis="a", recommendation="r")
    assert m.model_dump().keys() == FALLBACK_KEYS


def test_agent5_schema_forbids_extra_fields():
    """extra=forbid：不允许模型额外吐出 status/quantity 等决策字段。"""
    with pytest.raises(ValidationError):
        Agent5RiskAnalysis(summary="s", risk_level="HIGH", risk_analysis="a", recommendation="r", status="APPROVED")


# ---------- 成功路径 ----------

async def test_agent5_returns_structured_analysis(monkeypatch):
    class FakeStructured:
        async def ainvoke(self, prompt):
            return Agent5RiskAnalysis(
                summary="库存跌破安全线且报价偏高。",
                risk_level="HIGH",
                risk_analysis="当前库存低于安全线，3日需求无法满足。",
                recommendation="建议人工确认到货时效后放行。",
            )

    monkeypatch.setattr("app.graph.nodes.get_structured_llm", lambda schema: FakeStructured())
    state = _copy_review()
    result = await agent5_node(state)

    # 节点返回值只能声明 agent5_analysis 这一个键 → 结构上就无法改写 policy/status
    assert set(result) == {"agent5_analysis"}
    a5 = result["agent5_analysis"]
    assert a5["summary"].startswith("库存跌破安全线")
    assert a5["risk_level"] == "HIGH"
    assert a5["risk_analysis"]
    assert a5["recommendation"]
    assert set(a5) == FALLBACK_KEYS
    # 输入未被篡改
    assert state["policy_decision"]["status"] == "REVIEW"
    assert state["policy_decision"]["quantity"] == 20.0


# ---------- LLM 失败 fallback ----------

async def test_agent5_fallback_when_llm_fails(monkeypatch):
    def boom(schema):
        raise RuntimeError("ollama down")

    monkeypatch.setattr("app.graph.nodes.get_structured_llm", boom)
    result = await agent5_node(_copy_review())
    a5 = result["agent5_analysis"]
    assert a5["risk_level"] == "UNKNOWN"
    assert set(a5) == FALLBACK_KEYS
    assert set(result) == {"agent5_analysis"}  # 安全失败不改 policy / 不自动批准


# ---------- 非 REVIEW 防御 ----------

async def test_agent5_not_called_on_non_review(monkeypatch):
    def should_not_call(schema):
        raise AssertionError("非 REVIEW 不应调用 LLM")

    monkeypatch.setattr("app.graph.nodes.get_structured_llm", should_not_call)
    state = _copy_review()
    state["policy_decision"] = {**state["policy_decision"], "status": "PURCHASE"}
    result = await agent5_node(state)
    assert result["agent5_analysis"]["risk_level"] == "UNKNOWN"
