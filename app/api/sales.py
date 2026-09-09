"""点单销售 API（Phase 10）。

职责边界（薄 API 层，业务在 services/sales + services/inventory）：
- 只做：鉴权（RBAC）、输入校验、幂等键、InsufficientStock/ValueError → HTTP 映射。
- 不做：BOM 展开 / 金额计算 / 库存扣减 —— 全部在 service 与 consume_inventory。
- 不改采购闭环 / Agent / LangGraph；库存变化唯一经 stock_movements。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.idempotency import (
    check_idempotency,
    clear_idempotency,
    save_idempotency_result,
)
from app.core.security import require_roles
from app.services.inventory import InsufficientStock
from app.services.sales import (
    create_sales_order,
    list_recent_orders,
    list_saleable_menu,
)

router = APIRouter(prefix="/sales", tags=["sales"])

# 点单角色：order_clerk（点单员）为主，manager 可代客/演示下单；purchaser 无权（403）
_ORDER_ROLES = ["order_clerk", "manager"]


class SalesLineItem(BaseModel):
    dish_id: int = Field(..., description="菜品 id")
    qty: int = Field(..., gt=0, description="份数（必须 > 0）")


class CreateSalesOrderRequest(BaseModel):
    items: List[SalesLineItem] = Field(..., min_length=1, description="点单明细（至少 1 项）")


@router.get("/menu")
async def get_menu(
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_roles(_ORDER_ROLES)),
):
    """可售菜品清单（点单菜单）。"""
    return await list_saleable_menu(db)


@router.post("/orders")
async def post_sales_order(
    body: CreateSalesOrderRequest,
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_roles(_ORDER_ROLES)),
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key", min_length=8),
):
    """创建销售订单：BOM 展开 → 扣库存 → 写流水，单事务原子；缺料 409 零落库。"""
    old = await check_idempotency(x_idempotency_key)
    if old is not None:
        return old

    username = _auth.get("username")
    try:
        result = await create_sales_order(
            db,
            line_items=[{"dish_id": it.dish_id, "qty": it.qty} for it in body.items],
            created_by=username,
            order_type="ORDER_SALE",
        )
    except InsufficientStock as exc:
        # 整单原子拒绝（service 已 rollback）→ 缺料可重试：释放幂等键，抛 409 + 缺料清单
        await clear_idempotency(x_idempotency_key)
        raise HTTPException(
            status_code=409,
            detail={
                "message": "库存不足，整单已拒绝（未产生任何扣减或订单）",
                "missing": exc.missing,
            },
        )
    except ValueError as exc:
        await clear_idempotency(x_idempotency_key)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        await db.rollback()
        await clear_idempotency(x_idempotency_key)
        raise HTTPException(status_code=500, detail=f"创建点单失败: {exc}")

    await save_idempotency_result(x_idempotency_key, result)
    return result


@router.get("/orders")
async def get_sales_orders(
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_roles(_ORDER_ROLES)),
    limit: int = 20,
    order_type: Optional[str] = None,
):
    """最近 N 笔销售订单（含菜品明细）；响应带当前虚拟营业日。"""
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 须在 1..200")
    if order_type is not None and order_type not in ("ORDER_SALE", "SIMULATION_SALE"):
        raise HTTPException(status_code=400, detail=f"invalid order_type: {order_type}")
    from app.core.clock import get_current_date

    virtual_date = get_current_date()
    orders = await list_recent_orders(db, limit=limit, order_type=order_type)
    return {
        "virtual_date": virtual_date.isoformat(),
        "orders": orders,
    }
