import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from langgraph.types import Command

from app.core.clock import get_current_date
from app.core.db import get_db
from app.services.inventory import execute_inbound_stock
from app.core.idempotency import (
    check_idempotency,
    clear_idempotency,
    save_idempotency_result,
)
from app.core.lock import (
    acquire_approval_lock,
    release_approval_lock,
)

router = APIRouter(tags=["purchase"])

class PurchaseRequest(BaseModel):
    ingredient: str

class ApprovalRequest(BaseModel):
    approved: bool

class ApproveRequest(BaseModel):
    approved: bool
    approval_reason: Optional[str] = None

@router.post("/purchase")
async def create_purchase(
    request_body: PurchaseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key", min_length=8),
):
    old_result = await check_idempotency(x_idempotency_key)
    if old_result is not None:
        return old_result

    try:
        result = await db.execute(
            text(
                """
                SELECT
                    i.id AS ingredient_id,
                    i.name AS ingredient_name,
                    i.unit,
                    inv.current_stock,
                    inv.safety_stock,
                    inv.daily_sales,
                    s.id AS supplier_id,
                    s.name AS supplier_name,
                    s.current_price,
                    s.historical_avg_price
                FROM ingredients i
                JOIN inventory inv ON inv.ingredient_id = i.id
                JOIN suppliers s ON s.ingredient_id = i.id
                WHERE i.name = :ingredient
                LIMIT 1
                """
            ),
            {"ingredient": request_body.ingredient},
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Ingredient not found")

        order_no = "PO-" + uuid.uuid4().hex[:16].upper()
        thread_id = "purchase-" + uuid.uuid4().hex

        await db.execute(
            text(
                """
                INSERT INTO purchase_orders (order_no, thread_id, status, source, idempotency_key)
                VALUES (:order_no, :thread_id, 'RUNNING', 'MANUAL', :idempotency_key)
                """
            ),
            {
                "order_no": order_no,
                "thread_id": thread_id,
                "idempotency_key": x_idempotency_key,
            },
        )
        await db.commit()

        result = await db.execute(
            text("SELECT id FROM purchase_orders WHERE order_no = :order_no"),
            {"order_no": order_no},
        )
        order_id = int(result.scalar_one())

        graph = request.app.state.graph
        graph_state = {
            "order_id": order_id,
            "order_no": order_no,
            "thread_id": thread_id,
            "ingredient_id": int(row["ingredient_id"]),
            "ingredient": row["ingredient_name"],
            "unit": row["unit"],
            "current_stock": float(row["current_stock"]),
            "safety_stock": float(row["safety_stock"]),
            "daily_sales": float(row["daily_sales"]),
            "predicted_demand": 0.0,
            "quantity": 0.0,
            "supplier_id": int(row["supplier_id"]),
            "supplier_name": row["supplier_name"],
            "supplier_price": float(row["current_price"]),
            "historical_price": float(row["historical_avg_price"]),
            "price_deviation": 0.0,
            "total_amount": 0.0,
            "risk_reason": None,
            "approved": None,
            "status": "RUNNING",
        }

        config = {"configurable": {"thread_id": thread_id}}
        result = await graph.ainvoke(graph_state, config=config)

        if "__interrupt__" in result:
            interrupt_items = result["__interrupt__"]
            interrupt_value: dict[str, Any] = {}
            if interrupt_items:
                interrupt_value = interrupt_items[0].value

            total_amount = float(interrupt_value.get("total_amount", 0))
            quantity = float(interrupt_value.get("quantity", 0))
            supplier_price = float(interrupt_value.get("supplier_price", 0))
            reasons = interrupt_value.get("risk_reason", [])
            risk_reason = ",".join(reasons)

            await db.execute(
                text(
                    """
                    UPDATE purchase_orders
                    SET status = 'SUSPENDED', total_amount = :total_amount, risk_reason = :risk_reason
                    WHERE id = :order_id
                    """
                ),
                {"total_amount": total_amount, "risk_reason": risk_reason, "order_id": order_id},
            )

            await db.execute(
                text(
                    """
                    INSERT INTO purchase_order_items (order_id, ingredient_id, quantity, unit_price, total_price, supplier_id)
                    VALUES (:order_id, :ingredient_id, :quantity, :unit_price, :total_price, :supplier_id)
                    """
                ),
                {
                    "order_id": order_id,
                    "ingredient_id": int(row["ingredient_id"]),
                    "quantity": quantity,
                    "unit_price": supplier_price,
                    "total_price": total_amount,
                    "supplier_id": int(row["supplier_id"]),
                },
            )
            await db.commit()

            response = {
                "status": "SUSPENDED",
                "order_id": order_id,
                "order_no": order_no,
                "thread_id": thread_id,
                "interrupt": interrupt_value,
            }
            await save_idempotency_result(x_idempotency_key, response)
            return response

        final_status = result.get("status", "UNKNOWN")
        await db.execute(
            text(
                """
                UPDATE purchase_orders
                SET status = :status, total_amount = :total_amount
                WHERE id = :order_id
                """
            ),
            {
                "status": final_status,
                "total_amount": float(result.get("total_amount", 0)),
                "order_id": order_id,
            },
        )
        await db.commit()

        response = {
            "status": final_status,
            "order_id": order_id,
            "order_no": order_no,
            "thread_id": thread_id,
            "result": result,
        }
        await save_idempotency_result(x_idempotency_key, response)
        return response

    except HTTPException:
        await clear_idempotency(x_idempotency_key)
        raise
    except Exception as exc:
        await db.rollback()
        await clear_idempotency(x_idempotency_key)
        raise HTTPException(status_code=500, detail=str(exc))

@router.post("/approve/{order_id}")
async def approve_order(
    order_id: int,
    request_body: ApprovalRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    lock = await acquire_approval_lock(order_id)
    if lock is None:
        raise HTTPException(status_code=409, detail="Order is being approved")

    try:
        result = await db.execute(
            text("SELECT id, order_no, thread_id, status FROM purchase_orders WHERE id = :order_id LIMIT 1"),
            {"order_id": order_id},
        )
        order = result.mappings().first()

        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["status"] != "SUSPENDED":
            return {
                "status": order["status"],
                "order_id": order_id,
                "message": "Order is not waiting for approval",
            }

        graph = request.app.state.graph
        config = {"configurable": {"thread_id": order["thread_id"]}}

        result = await graph.ainvoke(
            Command(resume={"approved": request_body.approved}),
            config=config,
        )
        final_status = result.get("status", "UNKNOWN")

        update_result = await db.execute(
            text(
                """
                UPDATE purchase_orders
                SET status = :status
                WHERE id = :order_id AND status = 'SUSPENDED'
                """
            ),
            {"status": final_status, "order_id": order_id},
        )
        await db.commit()

        if update_result.rowcount != 1:
            raise HTTPException(status_code=409, detail="Order state changed during approval")

        return {
            "status": final_status,
            "order_id": order_id,
            "order_no": order["order_no"],
            "thread_id": order["thread_id"],
            "approved": request_body.approved,
            "result": result,
        }

    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Approval failed: {exc}")
    finally:
        await release_approval_lock(lock)


@router.post("/purchase/{order_id}/approve")
async def approve_purchase_order(
    order_id: int,
    request_body: ApproveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    lock = await acquire_approval_lock(order_id)
    if lock is None:
        raise HTTPException(status_code=409, detail="Order is being approved")

    try:
        result = await db.execute(
            text("SELECT id, order_no, thread_id, status FROM purchase_orders WHERE id = :order_id LIMIT 1"),
            {"order_id": order_id},
        )
        order = result.mappings().first()

        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["status"] not in ("SUSPENDED", "PENDING"):
            return {
                "status": order["status"],
                "order_id": order_id,
                "order_no": order["order_no"],
                "message": "Order is not waiting for approval",
                "approved": request_body.approved,
                "current_stock": await _get_current_stock(db, order_id),
            }

        item_result = await db.execute(
            text(
                """
                SELECT ingredient_id, quantity
                FROM purchase_order_items
                WHERE order_id = :order_id
                LIMIT 1
                """
            ),
            {"order_id": order_id},
        )
        item = item_result.mappings().first()

        if request_body.approved:
            # 0. 从数据库重建完整 PurchaseState（checkpoint 可能因 Redis 清空而缺失部分字段）
            ingredient_id = int(item["ingredient_id"]) if item else None
            quantity = float(item["quantity"]) if item else 0.0
            snapshot = await _load_resume_state(db, ingredient_id) if ingredient_id else {}

            # 1. 恢复 LangGraph 状态机（显式注入完整 state，保证即使 checkpoint 丢失也能 resume）
            graph = request.app.state.graph
            config = {"configurable": {"thread_id": order["thread_id"]}}
            resume_state = {
                "order_id": order_id,
                "order_no": order["order_no"],
                "thread_id": order["thread_id"],
                "status": "SUSPENDED",
            }
            resume_state.update(snapshot)
            resume_state["quantity"] = quantity
            resume_state["total_amount"] = float(
                (await db.execute(text("SELECT total_amount FROM purchase_orders WHERE id=:i"), {"i": order_id})).scalar() or 0
            )
            await graph.ainvoke(
                Command(resume={"approved": True}, update=resume_state),
                config=config,
            )

            # 1.5 记录人工审批原因与虚拟业务日期（与入库同一会话，随 execute_inbound_stock 一并 commit）
            await db.execute(
                text(
                    """
                    UPDATE purchase_orders
                    SET approval_reason = :approval_reason,
                        approved_virtual_date = :approved_virtual_date
                    WHERE id = :order_id AND status = 'SUSPENDED'
                    """
                ),
                {
                    "approval_reason": request_body.approval_reason,
                    "approved_virtual_date": get_current_date().isoformat(),
                    "order_id": order_id,
                },
            )

            # 2. 调用强绑定物理入库：库存累加 + 标 COMPLETED + 写 completed_at + commit
            inbound = await execute_inbound_stock(db, order_id)

            return {
                "status": "COMPLETED",
                "order_id": order_id,
                "order_no": order["order_no"],
                "thread_id": order["thread_id"],
                "approved": True,
                "approval_reason": request_body.approval_reason,
                "restocked_quantity": inbound.get("restocked_quantity", 0),
                "current_stock": inbound.get("current_stock"),
            }

        # approved == False -> REJECTED（approval_reason 保存审批时填写的业务原因）
        await db.execute(
            text(
                """
                UPDATE purchase_orders
                SET status = 'REJECTED',
                    approval_reason = :approval_reason,
                    rejected_virtual_date = :rejected_virtual_date
                WHERE id = :order_id AND status = 'SUSPENDED'
                """
            ),
            {
                "approval_reason": request_body.approval_reason,
                "rejected_virtual_date": get_current_date().isoformat(),
                "order_id": order_id,
            },
        )
        await db.commit()

        return {
            "status": "REJECTED",
            "order_id": order_id,
            "order_no": order["order_no"],
            "thread_id": order["thread_id"],
            "approved": False,
            "approval_reason": request_body.approval_reason,
            "current_stock": await _get_current_stock(db, order_id),
        }

    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Approval failed: {exc}")
    finally:
        await release_approval_lock(lock)


async def _get_current_stock(db: AsyncSession, order_id: int):
    result = await db.execute(
        text(
            """
            SELECT inv.current_stock AS stock
            FROM purchase_order_items poi
            JOIN inventory inv ON inv.ingredient_id = poi.ingredient_id
            WHERE poi.order_id = :order_id
            LIMIT 1
            """
        ),
        {"order_id": order_id},
    )
    row = result.mappings().first()
    return float(row["stock"]) if row else None


async def _load_resume_state(db: AsyncSession, ingredient_id: int) -> dict:
    """从数据库重建该食材的完整采购状态快照，供订单审批恢复状态机使用。

    当 LangGraph checkpoint（Redis）因重启/清空而缺失状态时，用数据库数据补齐，
    保证 resume 不需要依赖云端缓存即可找到所有必需字段。
    """
    result = await db.execute(
        text(
            """
            SELECT
                i.id AS ingredient_id,
                i.name AS ingredient_name,
                i.unit,
                inv.current_stock,
                inv.daily_sales,
                inv.safety_stock,
                s.id AS supplier_id,
                s.name AS supplier_name,
                s.current_price,
                s.historical_avg_price
            FROM ingredients i
            JOIN inventory inv ON inv.ingredient_id = i.id
            JOIN suppliers s ON s.ingredient_id = i.id
            WHERE i.id = :ingredient_id
            LIMIT 1
            """
        ),
        {"ingredient_id": ingredient_id},
    )
    row = result.mappings().first()
    if row is None:
        return {}

    price = float(row["current_price"])
    historical = float(row["historical_avg_price"])
    price_deviation = (abs(price - historical) / historical) if historical else 0.0
    daily_sales = float(row["daily_sales"])

    return {
        "ingredient_id": int(row["ingredient_id"]),
        "ingredient": row["ingredient_name"],
        "unit": row["unit"],
        "current_stock": float(row["current_stock"]),
        "safety_stock": float(row["safety_stock"]),
        "daily_sales": daily_sales,
        "predicted_demand": daily_sales * 3,
        "quantity": 0.0,
        "demand_reasoning": "",
        "supplier_id": int(row["supplier_id"]),
        "supplier_name": row["supplier_name"],
        "supplier_price": price,
        "historical_price": historical,
        "price_deviation": price_deviation,
        "total_amount": 0.0,
        "risk_analysis_report": None,
        "risk_reason": None,
        "approved": None,
    }
