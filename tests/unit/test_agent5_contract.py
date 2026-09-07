"""Phase 6-C Agent5 契约：normalize / Literal schema / 历史 checkpoint 兼容（MOCK）。

无基础设施；历史兼容用 InMemorySaver + StateGraph 复现"旧 dict 原样读回、不重建模型"。
"""
import pytest
from pydantic import ValidationError
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver

from app.graph.nodes import (
    Agent5RiskAnalysis,
    Agent5RiskAnalysisRaw,
    normalize_risk_level,
)
from app.graph.state import PurchaseState

FALLBACK_KEYS = {"summary", "risk_level", "risk_analysis", "recommendation"}


# ---------- normalize 映射表 ----------

@pytest.mark.parametrize("raw,expected", [
    ("HIGH", "HIGH"), ("high", "HIGH"),
    ("高", "HIGH"), ("中高", "HIGH"), ("严重", "HIGH"), ("极高", "HIGH"),
    ("MEDIUM", "MEDIUM"), ("medium", "MEDIUM"), ("中", "MEDIUM"), ("中等", "MEDIUM"),
    ("LOW", "LOW"), ("low", "LOW"), ("低", "LOW"),
    ("UNKNOWN", "UNKNOWN"), ("unknown", "UNKNOWN"), ("未知", "UNKNOWN"),
    ("  高  ", "HIGH"),  # trim
])
def test_normalize_mapping(raw, expected):
    assert normalize_risk_level(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "非法", "EXTREME", "very high", 123, None, 0.0, [], {}])
def test_normalize_fallback_unknown_never_raises(raw):
    assert normalize_risk_level(raw) == "UNKNOWN"


# ---------- Literal schema ----------

def test_canonical_schema_accepts_four_values_only():
    for v in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
        m = Agent5RiskAnalysis(summary="s", risk_level=v, risk_analysis="a", recommendation="r")
        assert m.model_dump()["risk_level"] == v


@pytest.mark.parametrize("bad", ["EXTREME", "中高", "高"])
def test_canonical_schema_rejects_non_canonical(bad):
    with pytest.raises(ValidationError):
        Agent5RiskAnalysis(summary="s", risk_level=bad, risk_analysis="a", recommendation="r")


def test_raw_schema_accepts_any_risk_level_for_extraction():
    m = Agent5RiskAnalysisRaw(summary="s", risk_level="中高", risk_analysis="a", recommendation="r")
    assert m.risk_level == "中高"  # 宽松提取阶段不受 Literal 限制
    assert set(m.model_dump()) == FALLBACK_KEYS


# ---------- 历史 checkpoint 兼容（MOCK：dict 原样读回，不重建模型） ----------

def _seed_node(risk_level: str):
    def seed(state: PurchaseState) -> dict:
        return {"agent5_analysis": {"summary": "s", "risk_level": risk_level,
                                    "risk_analysis": "a", "recommendation": "r"}}
    return seed


@pytest.mark.parametrize("old_value", ["中高", "高", "LOW"])
async def test_historical_checkpoint_reads_raw_dict_without_validation(old_value):
    """旧 checkpoint 中文/枚举值作为普通 dict 存读：restore 不触发 Literal 校验。"""
    saver = InMemorySaver()
    g = StateGraph(PurchaseState)
    g.add_node("seed", _seed_node(old_value))
    g.add_edge(START, "seed")
    g.add_edge("seed", END)
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": f"hist-{old_value}-1"}}
    await app.ainvoke({}, cfg)
    snap = await app.aget_state(cfg)
    a5 = snap.values.get("agent5_analysis")
    assert isinstance(a5, dict) and a5["risk_level"] == old_value  # 原样、无 ValidationError
