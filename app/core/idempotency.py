import json
from typing import Any, Optional
from fastapi import HTTPException
from app.core.redis import redis_client

IDEMPOTENCY_TTL = 600

def _redis_key(idempotency_key: str) -> str:
    return f"idempotency:{idempotency_key}"

async def check_idempotency(idempotency_key: str) -> Optional[dict[str, Any]]:
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="X-Idempotency-Key is required",
        )

    key = _redis_key(idempotency_key)
    processing_value = json.dumps({"status": "PROCESSING"}, ensure_ascii=False)

    acquired = await redis_client.set(
        key,
        processing_value,
        nx=True,
        ex=IDEMPOTENCY_TTL,
    )

    if acquired:
        return None

    raw = await redis_client.get(key)
    if raw is None:
        acquired = await redis_client.set(
            key,
            processing_value,
            nx=True,
            ex=IDEMPOTENCY_TTL,
        )
        if acquired:
            return None
        raise HTTPException(status_code=409, detail="Request is being processed")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=409, detail="Invalid idempotency state")

    status = data.get("status")
    if status == "PROCESSING":
        raise HTTPException(status_code=409, detail="Request is already processing")

    if status == "SUCCESS":
        return data.get("response")

    raise HTTPException(status_code=409, detail="Unknown idempotency state")

async def save_idempotency_result(idempotency_key: str, response: dict[str, Any]) -> None:
    key = _redis_key(idempotency_key)
    value = json.dumps({"status": "SUCCESS", "response": response}, ensure_ascii=False)
    await redis_client.set(key, value, ex=IDEMPOTENCY_TTL)

async def clear_idempotency(idempotency_key: str) -> None:
    await redis_client.delete(_redis_key(idempotency_key))
