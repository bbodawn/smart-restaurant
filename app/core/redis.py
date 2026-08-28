import os
import redis.asyncio as redis
from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

redis_client: Redis = redis.from_url(
    REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
    max_connections=50,
)

async def ping_redis() -> bool:
    return bool(await redis_client.ping())

async def close_redis() -> None:
    await redis_client.aclose()
