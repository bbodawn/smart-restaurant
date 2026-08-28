import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.api.intent import router as intent_router
from app.api.purchase import router as purchase_router
from app.core.db import close_db
from app.core.redis import close_redis, ping_redis
from app.graph.workflow import build_graph

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_ok = await ping_redis()
    if not redis_ok:
        raise RuntimeError("Failed to connect to Redis during startup")

    async with AsyncRedisSaver.from_conn_string(REDIS_URL) as checkpointer:
        await checkpointer.asetup()
        app.state.graph = build_graph(checkpointer)
        yield

    await close_redis()
    await close_db()

app = FastAPI(
    title="Smart Restaurant MVP",
    lifespan=lifespan,
)

app.include_router(purchase_router, prefix="/api/v1")
app.include_router(intent_router, prefix="/api/v1")

@app.get("/health")
async def health_check():
    return {"status": "ok"}
