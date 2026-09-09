"""销售点单服务（Phase 10）。

职责边界：
- 纯函数（expand_dish_consumption / compute_order_total）：确定性 BOM 展开与金额计算，
  离线可测、无 DB / LLM / Redis 依赖。
- create_sales_order：单事务落销售订单头/明细 + 调用 consume_inventory 扣库存写流水；
  任何缺料 → 整单原子回滚（订单与扣减零落库），库存变化唯一经 stock_movements。
- 只读查询：list_saleable_menu / list_recent_orders。

约束（Phase 10 冻结）：
- 不直接 UPDATE inventory —— 一律走 app.services.inventory.consume_inventory。
- 不改采购闭环 / Agent / LangGraph / 既有表语义。
"""
import uuid
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_current_date
from app.services.inventory import InsufficientStock, consume_inventory


# ---------- 纯函数：BOM 展开与金额（确定性、离线可测） ----------

def expand_dish_consumption(
    line_items: Sequence[Dict[str, Any]],
    dish_bom_rows: Sequence[Dict[str, Any]],
) -> Dict[int, float]:
    """把点单行按 dish_bom 展开为「按食材聚合的总消耗」。

    输入契约：
    - line_items: [{"dish_id": int, "qty": int}]
    - dish_bom_rows: [{"dish_id", "ingredient_id", "qty_per_serving"}]
    返回 {ingredient_id: 总消耗量}（跨菜品共用食材求和）。
    无 BOM 的菜品不产生消耗；qty<=0 的行忽略。
    """
    bom_by_dish: Dict[int, List[Dict[str, Any]]] = {}
    for row in dish_bom_rows:
        bom_by_dish.setdefault(int(row["dish_id"]), []).append(row)

    agg: Dict[int, float] = {}
    for line in line_items:
        dish_id = int(line["dish_id"])
        qty = int(line.get("qty") or 0)
        if qty <= 0:
            continue
        for row in bom_by_dish.get(dish_id, []):
            ing = int(row["ingredient_id"])
            agg[ing] = agg.get(ing, 0.0) + float(row["qty_per_serving"]) * qty
    # 浮点累计后统一收敛到 4 位，避免 0.30000000000000004 类噪声
    return {k: round(v, 4) for k, v in agg.items()}


def compute_order_total(
    line_items: Sequence[Dict[str, Any]],
    price_map: Dict[int, float],
) -> float:
    """订单金额 = Σ(售价 × 份数)；无行则 0。"""
    total = 0.0
    for line in line_items:
        dish_id = int(line["dish_id"])
        qty = int(line.get("qty") or 0)
        if qty <= 0:
            continue
        total += float(price_map.get(dish_id, 0.0)) * qty
    return round(total, 2)


def recent_order_sale_distribution(
    item_rows: Sequence[Dict[str, Any]],
) -> Dict[int, float]:
    """近窗口真实点单(ORDER_SALE)的各菜份数占比。

    输入：真实销售明细 [{dish_id, qty}]（调用方 SQL 只取 order_type='ORDER_SALE'）。
    返回 {dish_id: 份数占比}；占比对总份数归一；qty<=0 的行忽略。空输入 → {}。
    仅作模拟销量分摊的依据，绝不参与真实订单/库存扣减。
    """
    qty_by_dish: Dict[int, float] = {}
    total = 0.0
    for row in item_rows:
        dish_id = int(row["dish_id"])
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        qty_by_dish[dish_id] = qty_by_dish.get(dish_id, 0.0) + qty
        total += qty
    if total <= 0:
        return {}
    return {d: round(q / total, 6) for d, q in qty_by_dish.items()}


def simulate_sales_strategy(
    dish_ids: Sequence[int],
    daily_qty_map: Dict[int, int],
    recent_distribution: Dict[int, float],
) -> List[Dict[str, Any]]:
    """计算一个模拟营业日各菜的目标份数。

    语义（Section 4 冻结）：
    - 冷启动（recent_distribution 为空）→ 每菜份数 = 自身 simulation_daily_qty。
    - 有真实历史分布 → 总量 T = Σ simulation_daily_qty(候选菜)；只把 T 分摊给
      「本日候选且有历史销售」的菜（按占比），无历史/不在候选的菜不生成；
      整数分摊守恒（最后补差），避免总量放飞。
    返回 [{dish_id, qty}]，qty 均为正整份。
    """
    if not dish_ids:
        return []
    candidate = [int(d) for d in dish_ids]
    if not recent_distribution:
        return [{"dish_id": d, "qty": int(daily_qty_map.get(d, 0))} for d in candidate if int(daily_qty_map.get(d, 0)) > 0]

    # 有历史：只考虑候选内且有历史占比的菜；对其占比重新归一（若 dist 未归一的容错）
    subset = {d: float(s) for d, s in recent_distribution.items() if int(d) in candidate and float(s) > 0}
    if not subset:
        return [{"dish_id": d, "qty": int(daily_qty_map.get(d, 0))} for d in candidate if int(daily_qty_map.get(d, 0)) > 0]
    share_sum = sum(subset.values())
    total = float(sum(int(daily_qty_map.get(d, 0)) for d in candidate))

    # 整数分摊：先按 round 累加，最后一道菜补差使 Σqty == total
    out: List[Dict[str, Any]] = []
    assigned = 0.0
    subset_items = list(subset.items())
    for i, (dish_id, share) in enumerate(subset_items):
        raw = total * (share / share_sum)
        if i == len(subset_items) - 1:
            qty = max(1, int(total - assigned))  # 末位补差，保证守恒且至少 1 份
        else:
            qty = max(1, int(round(raw)))
        assigned += qty
        out.append({"dish_id": int(dish_id), "qty": qty})
    return out


# ---------- 点单创建（单事务 + consume_inventory） ----------

def compose_sales_order_no(virtual_date: date) -> str:
    """销售单号：SO-YYYYMMDD-<6位随机>（无计数器、无 SELECT MAX，天然唯一 + UNIQUE 兜底）。"""
    return f"SO-{virtual_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


async def create_sales_order(
    db: AsyncSession,
    *,
    line_items: Sequence[Dict[str, Any]],
    created_by: Optional[str],
    order_type: str = "ORDER_SALE",
    virtual_date: Optional[date] = None,
) -> Dict[str, Any]:
    """创建销售订单：单事务内 写订单头/明细 → consume_inventory 扣库存记流水 → commit。

    事务边界：
    - 全部写在同一 AsyncSession 事务；consume_inventory 不自行 commit（Phase 10 决策），
      由本函数统一 commit —— 缺料时其内部 rollback 会把本函数未提交的订单头/明细一并回滚，
      实现「整单原子拒绝、订单零落库」。
    - 库存不足抛 InsufficientStock（API 层映射 409）。
    - 库存变化唯一入口：consume_inventory（写 stock_movements + 更新 inventory）。
    """
    if not line_items:
        raise ValueError("line_items 不能为空")
    if virtual_date is None:
        virtual_date = get_current_date()
    for line in line_items:
        if int(line.get("qty") or 0) <= 0:
            raise ValueError(f"dish_id={line.get('dish_id')} qty 必须 > 0")

    dish_ids = [int(l["dish_id"]) for l in line_items]
    marks = ",".join(f":d{n}" for n in range(len(dish_ids)))
    params = {f"d{n}": d for n, d in enumerate(dish_ids)}

    # 1. 读菜品价格与可售状态（只接受可售菜；不可售/不存在一律按缺失拒绝，绝不空扣成交）
    dish_rows = (
        await db.execute(
            text(
                f"""
                SELECT id, name, price, available
                FROM dishes
                WHERE id IN ({marks}) AND available = 1
                """
            ),
            params,
        )
    ).mappings().all()
    dish_map = {r["id"]: dict(r) for r in dish_rows}
    missing_ids = [d for d in dish_ids if d not in dish_map]
    if missing_ids:
        raise ValueError(f"dish 不存在: {missing_ids}")

    price_map = {did: float(dish_map[did]["price"]) for did in dish_map}

    # 2. 读这些菜的 BOM 行（该菜必须可用，否则视为无菜可售）
    bom_rows = (
        await db.execute(
            text(
                f"""
                SELECT db.dish_id, db.ingredient_id, db.qty_per_serving
                FROM dish_bom db
                JOIN dishes d ON d.id = db.dish_id
                WHERE db.dish_id IN ({marks}) AND d.available = 1
                """
            ),
            params,
        )
    ).mappings().all()
    bom_rows = [dict(r) for r in bom_rows]

    # 3. 确定性计算：金额 + 按食材聚合消耗
    total_amount = compute_order_total(line_items, price_map)
    consumption = expand_dish_consumption(line_items, bom_rows)

    order_no = compose_sales_order_no(virtual_date)

    # 4. 写订单头（同事务，未 commit）
    await db.execute(
        text(
            """
            INSERT INTO sales_orders
                (order_no, order_type, status, total_amount, virtual_date, created_by)
            VALUES
                (:order_no, :order_type, 'COMPLETED', :total_amount, :virtual_date, :created_by)
            """
        ),
        {
            "order_no": order_no,
            "order_type": order_type,
            "total_amount": total_amount,
            "virtual_date": virtual_date.isoformat(),
            "created_by": created_by,
        },
    )
    order_id = int(
        (await db.execute(text("SELECT id FROM sales_orders WHERE order_no = :o"), {"o": order_no})).scalar_one()
    )

    # 5. 写订单明细（一菜一行，售价快照）
    for line in line_items:
        did = int(line["dish_id"])
        qty = int(line["qty"])
        unit_price = price_map[did]
        await db.execute(
            text(
                """
                INSERT INTO sales_order_items (order_id, dish_id, qty, unit_price, subtotal)
                VALUES (:order_id, :dish_id, :qty, :unit_price, :subtotal)
                """
            ),
            {
                "order_id": order_id,
                "dish_id": did,
                "qty": qty,
                "unit_price": unit_price,
                "subtotal": round(unit_price * qty, 2),
            },
        )

    # 6. 唯一出库入口：consume_inventory（写 stock_movements + 更新 inventory）
    #    缺料时内部 rollback 并抛 InsufficientStock → 上层不 commit，订单头/明细一并回滚。
    if consumption:
        await consume_inventory(
            db,
            [{"ingredient_id": k, "qty": v} for k, v in consumption.items()],
            movement_type=order_type,   # ORDER_SALE / SIMULATION_SALE
            reference_type="SALES_ORDER",
            reference_id=order_id,
            virtual_date=virtual_date,
            note=f"点单 {order_no}",
        )

    await db.commit()

    items_out = [
        {
            "dish_id": int(l["dish_id"]),
            "dish_name": dish_map[int(l["dish_id"])]["name"],
            "qty": int(l["qty"]),
            "unit_price": float(dish_map[int(l["dish_id"])]["price"]),
            "subtotal": round(float(dish_map[int(l["dish_id"])]["price"]) * int(l["qty"]), 2),
        }
        for l in line_items
    ]

    return {
        "order_id": order_id,
        "order_no": order_no,
        "order_type": order_type,
        "status": "COMPLETED",
        "total_amount": total_amount,
        "virtual_date": virtual_date.isoformat(),
        "created_by": created_by,
        "items": items_out,
    }


# ---------- 只读查询 ----------

async def list_saleable_menu(db: AsyncSession) -> List[Dict[str, Any]]:
    """可售菜品清单（点单菜单）。"""
    rows = (
        await db.execute(
            text(
                """
                SELECT id, name, category, price
                FROM dishes
                WHERE available = 1
                ORDER BY id
                """
            )
        )
    ).mappings().all()
    return [{"dish_id": r["id"], "name": r["name"], "category": r["category"], "price": float(r["price"])} for r in rows]


async def list_recent_orders(
    db: AsyncSession,
    *,
    limit: int = 20,
    order_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """最近 N 笔销售订单（含菜品明细），供点单员今日复核。"""
    cond, p = [], {}
    if order_type:
        cond.append("so.order_type = :ot")
        p["ot"] = order_type
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    orders = (
        await db.execute(
            text(
                f"""
                SELECT so.id, so.order_no, so.order_type, so.status, so.total_amount,
                       so.virtual_date, so.created_by, so.created_at
                FROM sales_orders so
                {where}
                ORDER BY so.id DESC
                LIMIT :limit
                """
            ),
            {**p, "limit": int(limit)},
        )
    ).mappings().all()

    out: List[Dict[str, Any]] = []
    for o in orders:
        items = (
            await db.execute(
                text(
                    """
                    SELECT d.name AS dish_name, soi.qty, soi.unit_price, soi.subtotal
                    FROM sales_order_items soi
                    JOIN dishes d ON d.id = soi.dish_id
                    WHERE soi.order_id = :oid
                    ORDER BY soi.id
                    """
                ),
                {"oid": o["id"]},
            )
        ).mappings().all()
        out.append(
            {
                "order_id": o["id"],
                "order_no": o["order_no"],
                "order_type": o["order_type"],
                "status": o["status"],
                "total_amount": float(o["total_amount"]),
                "virtual_date": o["virtual_date"].isoformat() if o["virtual_date"] else None,
                "created_by": o["created_by"],
                "created_at": o["created_at"].isoformat() if o["created_at"] else None,
                "items": [
                    {
                        "dish_name": i["dish_name"],
                        "qty": i["qty"],
                        "unit_price": float(i["unit_price"]),
                        "subtotal": float(i["subtotal"]),
                    }
                    for i in items
                ],
            }
        )
    return out
