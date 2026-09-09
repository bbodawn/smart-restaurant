"""销售 BOM 展开核心契约（纯同步、无基础设施依赖）。

目标：把点单闭环的确定性计算钉死为不可回归契约：
- expand_dish_consumption：菜品行 × dish_bom → 按食材聚合总消耗（跨菜品共用食材求和）
- compute_order_total：订单金额 = Σ(菜品售价 × 份数)

这些纯函数是 consume_inventory 调用的唯一前置，BOM 展开错误会直接导致库存扣错/金额错，
因此必须在无 DB 环境离线回归。
"""
import pytest

from app.services.sales import compute_order_total, expand_dish_consumption

# dish_bom 行契约：dish_id / ingredient_id / qty_per_serving
BOM_ROWS = [
    # 土豆炖牛肉(dish1)
    {"dish_id": 1, "ingredient_id": 8, "qty_per_serving": 0.20},   # 高山土豆
    {"dish_id": 1, "ingredient_id": 5, "qty_per_serving": 0.15},   # 雪花牛肉
    # 白灼基围虾(dish2)
    {"dish_id": 2, "ingredient_id": 6, "qty_per_serving": 0.25},
    # 京葱炒肉(dish3) —— 与 dish1 共用…… 不共用，此处为验证跨菜品共用单独造
]


def test_expand_aggregates_single_dish_consumption():
    lines = [{"dish_id": 1, "qty": 2}]
    out = expand_dish_consumption(lines, BOM_ROWS)
    assert out == {8: 0.4, 5: 0.3}  # 0.20*2, 0.15*2


def test_expand_aggregates_multi_dish_and_cross_dish_same_ingredient():
    # dish2(基围虾0.25/份) ×3 + dish1(土豆0.20/份) ×2 —— 不共用食材，验证多菜独立展开
    lines = [{"dish_id": 1, "qty": 2}, {"dish_id": 2, "qty": 3}]
    out = expand_dish_consumption(lines, BOM_ROWS)
    assert out == {8: 0.4, 5: 0.3, 6: 0.75}


def test_expand_cross_dish_same_ingredient_sums():
    # 两个不同菜品都含 ingredient 99（每份 0.1 / 0.2）→ 必须聚合为 0.1*x + 0.2*y
    rows = [
        {"dish_id": 10, "ingredient_id": 99, "qty_per_serving": 0.10},
        {"dish_id": 11, "ingredient_id": 99, "qty_per_serving": 0.20},
    ]
    lines = [{"dish_id": 10, "qty": 3}, {"dish_id": 11, "qty": 2}]
    assert expand_dish_consumption(lines, rows) == {99: 0.7}


def test_expand_dish_without_bom_row_yields_empty():
    lines = [{"dish_id": 999, "qty": 2}]  # 无 BOM 行
    assert expand_dish_consumption(lines, BOM_ROWS) == {}


def test_expand_ignores_lines_with_zero_qty():
    lines = [{"dish_id": 1, "qty": 0}]  # 0 份不消耗
    assert expand_dish_consumption(lines, BOM_ROWS) == {}


def test_compute_total_sum_price_x_qty():
    price_map = {1: 38.0, 2: 48.0, 3: 3.0}
    lines = [{"dish_id": 1, "qty": 2}, {"dish_id": 3, "qty": 4}]
    assert compute_order_total(lines, price_map) == 38.0 * 2 + 3.0 * 4


def test_compute_total_rounds_two_decimals():
    price_map = {1: 0.1, 2: 0.2}
    lines = [{"dish_id": 1, "qty": 1}, {"dish_id": 2, "qty": 3}]
    assert compute_order_total(lines, price_map) == pytest.approx(0.7)


def test_compute_total_empty_lines_is_zero():
    assert compute_order_total([], {}) == 0.0
