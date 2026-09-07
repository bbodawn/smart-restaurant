"""入库管理 · 采购订单展示只读 API（Phase 8）。

定位：业务执行视图（采购订单生命周期/审核/入库），与经营分析 Dashboard 分离。
只读；所有字段来自既有表（purchase_orders / purchase_order_items / ingredients /
suppliers / inbound_records），不新增 DB 字段、不改表结构、不改 Dashboard。

派生规则（固定，见 Phase 8-B）：
- 审核人 reviewer_display / 审核状态 approval_status：
    SUSPENDED          -> ('等待审核', '-')
    COMPLETED+系统     -> ('自动审核', '系统')    # source=AUTO 且无人工 reason（或 system auto-approve）
    COMPLETED+人工reason-> ('已通过', '人工审核')
    REJECTED           -> ('已拒绝', '人工审核')
- 入库状态 inbound_status：
    SUSPENDED -> 待审核；COMPLETED 且有 inbound -> 已入库；COMPLETED 无 inbound -> 采购完成(历史订单)；
    REJECTED -> 已拒绝。不伪造运输/到货等状态。
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db

router = APIRouter(prefix="/inbound", tags=["inbound"])

_SELECT = """
    SELECT po.id, po.order_no, po.status, po.source, po.total_amount,
           po.created_at, po.approval_reason,
           po.approved_virtual_date, po.rejected_virtual_date,
           po.agent5_summary, po.agent5_risk_level,
           po.agent5_risk_analysis, po.agent5_recommendation,
           poi.quantity, poi.unit_price,
           i.name AS ingredient, i.unit, i.category,
           s.name AS supplier_name, s.current_price AS supplier_quote,
           (SELECT COUNT(*) FROM inbound_records ir WHERE ir.order_id = po.id) AS inbound_cnt,
           (SELECT ir.inbound_virtual_date FROM inbound_records ir
             WHERE ir.order_id = po.id ORDER BY ir.id DESC LIMIT 1) AS inbound_virtual_date
    FROM purchase_orders po
    LEFT JOIN purchase_order_items poi ON poi.order_id = po.id
    LEFT JOIN ingredients i ON i.id = poi.ingredient_id
    LEFT JOIN suppliers s ON s.id = poi.supplier_id
"""

_VALID_STATUS = {"RUNNING", "SUSPENDED", "COMPLETED", "REJECTED"}
_VALID_SOURCE = {"AUTO", "MANUAL"}


def _approval(source: str, status: str, approval_reason: Optional[str]):
    if status == "SUSPENDED":
        return ("等待审核", "-")
    if status == "REJECTED":
        return ("已拒绝", "人工审核")
    if status == "COMPLETED":
        reason = approval_reason or ""
        system_auto = source == "AUTO" and (not reason or reason.startswith("system auto-approve"))
        if system_auto:
            return ("自动审核", "系统")
        return ("已通过", "人工审核")
    return (status, None)


def _inbound_status(status: str, inbound_cnt: int) -> str:
    if status == "SUSPENDED":
        return "待审核"
    if status == "COMPLETED":
        return "已入库" if inbound_cnt and inbound_cnt > 0 else "采购完成(历史订单)"
    if status == "REJECTED":
        return "已拒绝"
    return status


def _serialize(row) -> Dict[str, Any]:
    source = row["source"] or ""
    status = row["status"] or ""
    approval_status, reviewer_display = _approval(source, status, row["approval_reason"])
    agent5 = None
    if row["agent5_summary"] is not None or row["agent5_risk_level"] is not None:
        agent5 = {
            "risk_level": row["agent5_risk_level"],
            "summary": row["agent5_summary"],
            "risk_analysis": row["agent5_risk_analysis"],
            "recommendation": row["agent5_recommendation"],
        }
    inbound_cnt = int(row["inbound_cnt"] or 0)
    return {
        "order_id": row["id"],
        "order_no": row["order_no"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "status": status,
        "source": source,
        # 食材信息
        "ingredient": row["ingredient"],
        "category": row["category"],
        "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
        "unit": row["unit"] or "kg",
        "unit_price": float(row["unit_price"]) if row["unit_price"] is not None else None,
        "total_amount": float(row["total_amount"]) if row["total_amount"] else 0.0,
        # 供应商
        "supplier_name": row["supplier_name"],
        "supplier_quote": float(row["supplier_quote"]) if row["supplier_quote"] is not None else None,
        # 风险分析（MySQL agent5_* 快照）
        "agent5_analysis": agent5,
        # 审批
        "approval_status": approval_status,
        "reviewer_display": reviewer_display,
        "approval_reason": row["approval_reason"],
        "approved_virtual_date": row["approved_virtual_date"].isoformat() if row["approved_virtual_date"] else None,
        "rejected_virtual_date": row["rejected_virtual_date"].isoformat() if row["rejected_virtual_date"] else None,
        # 入库
        "inbound_status": _inbound_status(status, inbound_cnt),
        "inbound_virtual_date": row["inbound_virtual_date"].isoformat() if row["inbound_virtual_date"] else None,
    }


@router.get("/orders")
async def list_inbound_orders(
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    """采购订单列表（入库管理视图）。可选按 status / source 过滤。"""
    if status is not None and status not in _VALID_STATUS:
        raise HTTPException(status_code=400, detail=f"invalid status: {status}")
    if source is not None and source not in _VALID_SOURCE:
        raise HTTPException(status_code=400, detail=f"invalid source: {source}")

    sql = _SELECT
    cond, params = [], {}
    if status:
        cond.append("po.status = :status")
        params["status"] = status
    if source:
        cond.append("po.source = :source")
        params["source"] = source
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY po.id DESC"

    result = await db.execute(text(sql), params)
    return [_serialize(r) for r in result.mappings()]


@router.get("/orders/{order_id}")
async def inbound_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """单个采购订单详情（同一行对象）。"""
    result = await db.execute(text(_SELECT + " WHERE po.id = :order_id"), {"order_id": order_id})
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return _serialize(row)
