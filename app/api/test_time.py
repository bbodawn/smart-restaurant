from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.clock import get_current_date, reset_virtual_clock
from app.services.time_simulation import simulate_days_passing

router = APIRouter(prefix="/test", tags=["test-time-travel"])

class AdvanceTimeRequest(BaseModel):
    days: int = Field(default=1, ge=1, le=30, description="需要快进的天数（1-30天）")

@router.get("/clock-status")
async def get_clock_status():
    return {
        "current_virtual_date": get_current_date().isoformat()
    }

@router.post("/advance-day")
async def advance_time_and_simulate(
    request: AdvanceTimeRequest,
    db: AsyncSession = Depends(get_db)
):
    try:
        res = await simulate_days_passing(db, days=request.days)
        return {
            "message": f"Successfully advanced {request.days} day(s).",
            "data": res
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to advance time: {str(e)}")

@router.post("/reset-clock")
async def reset_clock():
    current = reset_virtual_clock()
    return {
        "message": "Virtual clock reset to system today",
        "current_virtual_date": current.isoformat()
    }
