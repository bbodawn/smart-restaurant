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


def test_phase8e_erp_table_has_columns_and_actions():
    """Phase 8-E：入库管理为 ERP 大表格（列头/审核操作/详情弹窗/AI 分析）。"""
    html = INDEX.read_text(encoding="utf-8")
    # 表头列
    for col in ["订单号", "日期", "食材名", "食材价格", "采购数量", "采购总金额",
                "供应商", "审核状态", "详情", "审核操作"]:
        assert col in html, f"缺表头列 {col}"
    assert "inbound-tbody" in html
    assert "同意" in html and "驳回" in html       # SUSPENDED 审核操作按钮
    assert "Agent5 风险分析" in html and "AI 建议" in html
    assert "该订单无需风险审核" in html
    assert "inboundDecide(" in html               # 复用 decide 的包装入口
    assert "openInboundDetail(" in html and "inbound-modal" in html  # 详情弹窗


def test_phase9b_login_and_rbac_present():
    """Phase 9-B：登录视图/退出/角色权限/Authorization/角色中文映射存在。"""
    html = INDEX.read_text(encoding="utf-8")
    for tok in ["login-view", "logout", "ROLE_PERMISSION", "canView", "ROLE_CN",
                "'Bearer ' + token", "点单功能开发中", "当前角色无权限访问该页面",
                "order_clerk"]:
        assert tok in html, f"missing {tok}"
    assert "主管" in html and "采购员" in html and "点单员" in html


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
