from typing import Optional
from redis.asyncio.lock import Lock
from app.core.redis import redis_client

APPROVAL_LOCK_TIMEOUT = 30
APPROVAL_LOCK_BLOCKING_TIMEOUT = 3

# Auto Procurement 按 ingredient 的互斥锁（Phase 4 Production Hardening）
AUTO_INGREDIENT_LOCK_TIMEOUT = 60
AUTO_INGREDIENT_LOCK_BLOCKING_TIMEOUT = 0

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

async def acquire_ingredient_lock(ingredient_id: int) -> Optional[Lock]:
    """Auto Procurement 同一 ingredient 的互斥锁（Phase 4）。

    非阻塞（blocking_timeout=0）：拿不到即返回 None，由调用方把该 ingredient
    交由其它 worker 处理（跳过），避免阻塞整个巡检。key 为 ingredient 粒度，
    不同 ingredient 之间互不阻塞；与 approval lock(order_id) 分属两个并发域。
    """
    lock = redis_client.lock(
        name=f"lock:auto-procurement:ingredient:{ingredient_id}",
        timeout=AUTO_INGREDIENT_LOCK_TIMEOUT,
        blocking_timeout=AUTO_INGREDIENT_LOCK_BLOCKING_TIMEOUT,
        sleep=0.1,
        thread_local=True,
    )
    acquired = await lock.acquire()
    if not acquired:
        return None
    return lock

async def release_ingredient_lock(lock: Optional[Lock]) -> None:
    if lock is None:
        return
    try:
        await lock.release()
    except Exception:
        pass
