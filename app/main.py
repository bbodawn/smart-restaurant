import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.api.dashboard import router as dashboard_router
from app.api.intent import router as intent_router
from app.api.purchase import router as purchase_router
from app.api.test_time import router as test_time_router
from app.core.db import close_db
from app.core.redis import close_redis, ping_redis
from app.graph.workflow import build_graph

WEB_INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")

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
app.include_router(test_time_router, prefix="/api/v1")
app.include_router(dashboard_router, prefix="/api/v1")

@app.get("/", include_in_schema=False)
async def dashboard_page():
    return FileResponse(WEB_INDEX)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
