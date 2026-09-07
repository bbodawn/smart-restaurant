"""前端展示契约（静态检查，不运行浏览器）。

钉死 index.html 里「智能风控分析」的渲染顺序与边界：
- agent5_analysis 存在 → 优先展示（summary/risk_level/risk_analysis/recommendation）
- 缺失 → 回退 legacy risk_analysis_report
- 一律经 escTxt 转义
- 不展示内部机器字段 risk_flags / risk_reason 原文
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "app" / "web" / "index.html"


def _render_block() -> str:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function renderRiskContent")
    end = html.index("function orderCard", start)
    return html[start:end]


def test_index_exists():
    assert INDEX.is_file()


def test_agent5_analysis_is_rendered_first_with_escape():
    block = _render_block()
    # 先出现 agent5 分支，且输出经过 escTxt
    assert "o.agent5_analysis" in block
    assert "escTxt(a.summary)" in block
    assert "escTxt(a.risk_analysis)" in block
    assert "escTxt(a.recommendation)" in block
    assert "riskLevelCN(a.risk_level)" in block or "riskLevelCN" in block


def test_legacy_risk_analysis_report_is_fallback():
    block = _render_block()
    assert "o.risk_analysis_report" in block  # fallback 仍保留


def test_internal_fields_not_rendered_in_frontend():
    """前端不应把内部审计字段原文渲染给用户。"""
    html = INDEX.read_text(encoding="utf-8")
    assert "risk_flags" not in html
    assert "risk_reason" not in html


def test_phase8d_workbench_card_has_actions_and_ai_block():
    """Phase 8-D：入库工作台卡片含审核按钮与 AI 风险分析块（ERP 审核页形态）。"""
    html = INDEX.read_text(encoding="utf-8")
    assert "批准采购" in html
    assert "拒绝采购" in html
    assert "AI 风险分析" in html
    assert "AI 建议" in html
    assert "该订单无需风险审核" in html
    assert "inboundDecide(" in html      # 复用 decide 的包装入口


def test_source_and_approval_labels_rendered():
    """Phase 6-D：采购方式/供应商/审批意见在订单卡可见（文案经 escTxt）。"""
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("function orderCard")
    end = html.index("function renderOrders", start)
    card = html[start:end]
    assert "🤖 自动采购" in card
    assert "👤 人工审批" in card
    assert "o.supplier_name" in card
    assert "o.unit_price" in card
    assert "escTxt(o.approval_reason)" in card
    assert "escTxt(o.approved_virtual_date)" in card
    assert "审批意见：" in card
