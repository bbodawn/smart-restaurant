"""SIMULATION_SALE 模拟销量策略（纯同步、无基础设施依赖）。

意图：快进生成的模拟订单量必须确定、可回归：
- recent_order_sale_distribution：只消费真实 ORDER_SALE 明细 → 各菜份数占比。
- simulate_sales_strategy：
  · 冷启动（无历史分布）→ 每菜取自身 simulation_daily_qty；
  · 有历史 → 固定总量(Σ simulation_daily_qty) × 真实占比分摊；
  · 分摊做整数收敛，总量保持 ≈ Σ simulation_daily_qty（不放飞）。
"""
import pytest

from app.services.sales import recent_order_sale_distribution, simulate_sales_strategy

DAILY = {1: 20, 2: 12, 3: 15, 4: 40}  # dish_id -> simulation_daily_qty（示意：菜1..4）


# ---------- recent_order_sale_distribution ----------

def test_distribution_empty_input_yields_empty():
    assert recent_order_sale_distribution([]) == {}


def test_distribution_normalizes_share_by_qty():
    rows = [
        {"dish_id": 1, "qty": 10},
        {"dish_id": 1, "qty": 10},   # 菜1 共 20 份
        {"dish_id": 2, "qty": 5},    # 菜2 共 5 份
    ]
    dist = recent_order_sale_distribution(rows)
    assert dist[1] == pytest.approx(20 / 25)  # 0.8
    assert dist[2] == pytest.approx(5 / 25)   # 0.2


def test_distribution_ignores_rows_with_nonpositive_qty():
    rows = [
        {"dish_id": 1, "qty": 10},
        {"dish_id": 2, "qty": 0},
        {"dish_id": 3, "qty": -3},
    ]
    dist = recent_order_sale_distribution(rows)
    assert set(dist) == {1}
    assert dist[1] == pytest.approx(1.0)


# ---------- simulate_sales_strategy ----------

def test_strategy_cold_start_uses_simulation_daily_qty():
    # 无历史分布 → 每菜目标份数 = 自身 simulation_daily_qty
    out = simulate_sales_strategy(list(DAILY.keys()), DAILY, {})
    assert {o["dish_id"]: o["qty"] for o in out} == DAILY


def test_strategy_with_history_splits_fixed_total_by_share():
    # 有历史分布：菜1占80%、菜2占20%、菜3/4 无历史
    dist = {1: 0.8, 2: 0.2}
    out = simulate_sales_strategy(list(DAILY.keys()), DAILY, dist)
    mapping = {o["dish_id"]: o["qty"] for o in out}
    total = sum(DAILY.values())  # 87
    # 总量恒定：只分摊给有历史的菜；无历史菜(3/4)不生成
    assert mapping.get(1) == pytest.approx(round(0.8 * total))
    assert mapping.get(2) == pytest.approx(round(0.2 * total))
    assert 3 not in mapping and 4 not in mapping
    assert sum(mapping.values()) == pytest.approx(total)


def test_strategy_never_exceeds_total_with_lopsided_history():
    # 极端分布(菜1=100%) → 只生成菜1、份数=总量
    out = simulate_sales_strategy(list(DAILY.keys()), DAILY, {1: 1.0})
    mapping = {o["dish_id"]: o["qty"] for o in out}
    assert set(mapping) == {1}
    assert mapping[1] == sum(DAILY.values())


def test_strategy_with_unknown_dish_ids_in_history_is_ignored():
    # 历史里有不在菜谱的 dish（已下架）→ 忽略，不影响总量再分配
    dist = {1: 0.5, 999: 0.5}
    out = simulate_sales_strategy([1], {1: 20}, dist)
    assert out == [{"dish_id": 1, "qty": 20}]
