"""三个确定性分析节点输出契约（纯同步、无基础设施依赖）。

目标：钉死「事实层」写给 policy 的数据形状，防止字段名/口径漂移。
"""
import pytest

from app.graph.nodes import (
    inventory_analysis_node,
    price_analysis_node,
    supplier_analysis_node,
)

INVENTORY_KEYS = {
    "current_stock",
    "daily_sales",
    "safety_stock",
    "predicted_3_day_demand",
    "stock_coverage_days",
    "health_status",
}
PRICE_KEYS = {"supplier_price", "historical_price", "price_deviation", "price_status"}
SUPPLIER_KEYS = {"supplier_id", "supplier_name", "supplier_price"}


def _base(**over):
    state = {
        "ingredient": "测试食材",
        "unit": "kg",
        "current_stock": 0.0,
        "daily_sales": 0.0,
        "safety_stock": 0.0,
        "supplier_id": 1,
        "supplier_name": "测试供应商",
        "supplier_price": 0.0,
        "historical_price": 0.0,
    }
    state.update(over)
    return state


def test_inventory_health_mapping_healthy():
    out = inventory_analysis_node(_base(current_stock=50, daily_sales=10, safety_stock=12))
    assert out["analysis_inventory"]["predicted_3_day_demand"] == 30.0
    assert out["analysis_inventory"]["stock_coverage_days"] == 5.0
    assert out["analysis_inventory"]["health_status"] == "HEALTHY"
    assert set(out["analysis_inventory"]) == INVENTORY_KEYS


def test_inventory_health_mapping_low_stock():
    out = inventory_analysis_node(_base(current_stock=20, daily_sales=10, safety_stock=12))
    assert out["analysis_inventory"]["health_status"] == "LOW_STOCK"  # 低于3日需求但高于安全线


def test_inventory_health_mapping_below_safety():
    out = inventory_analysis_node(_base(current_stock=10, daily_sales=10, safety_stock=12))
    assert out["analysis_inventory"]["health_status"] == "BELOW_SAFETY"


def test_inventory_coverage_days_none_when_no_sales():
    """无日消耗时不存在有意义的覆盖天数（None），且不构成 3~7 天规则。"""
    out = inventory_analysis_node(_base(current_stock=50, daily_sales=0, safety_stock=10))
    assert out["analysis_inventory"]["stock_coverage_days"] is None


def test_price_deviation_status_within_20pct():
    out = price_analysis_node(_base(supplier_price=25, historical_price=20))
    a = out["analysis_price"]
    assert a["price_deviation"] == pytest.approx(0.25, abs=1e-4)
    assert a["price_status"] == "GT_20PCT"
    assert set(a) == PRICE_KEYS


def test_price_deviation_status_no_historical():
    out = price_analysis_node(_base(supplier_price=30, historical_price=0))
    a = out["analysis_price"]
    assert a["price_deviation"] == 0.0
    assert a["price_status"] == "NO_HISTORICAL_PRICE"  # 不除零


def test_supplier_snapshot_passthrough():
    out = supplier_analysis_node(_base(supplier_id=7, supplier_name="华东供应商", supplier_price=12.5))
    a = out["analysis_supplier"]
    assert a["supplier_id"] == 7
    assert a["supplier_name"] == "华东供应商"
    assert a["supplier_price"] == 12.5
    assert set(a) == SUPPLIER_KEYS
