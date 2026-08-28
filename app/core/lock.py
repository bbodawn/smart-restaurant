from typing import Optional
from redis.asyncio.lock import Lock
from app.core.redis import redis_client

APPROVAL_LOCK_TIMEOUT = 30
APPROVAL_LOCK_BLOCKING_TIMEOUT = 3

async def acquire_approval_lock(order_id: int) -> Optional[Lock]:
    lock = redis_client.lock(
        name=f"lock:approval:{order_id}",
        timeout=APPROVAL_LOCK_TIMEOUT,
        blocking_timeout=APPROVAL_LOCK_BLOCKING_TIMEOUT,
        sleep=0.1,
        thread_local=True,
    )
    acquired = await lock.acquire()
    if not acquired:
        return None
    return lock

async def release_approval_lock(lock: Optional[Lock]) -> None:
    if lock is None:
        return
    try:
        await lock.release()
    except Exception:
        pass
