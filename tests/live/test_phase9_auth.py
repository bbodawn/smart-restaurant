"""Phase 9-A：登录 + RBAC（LIVE HTTP）。

覆盖：三角色登录成功、错误密码 401、approve 权限（manager/purchaser 200 语义、order_clerk 403）。
"""
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from app.core.db import get_db

pytestmark = pytest.mark.live


@pytest_asyncio.fixture
async def client(_session_factory):
    from app.api.auth import router as auth_router
    from app.api.purchase import router as purchase_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(purchase_router, prefix="/api/v1")

    async def _override():
        async with _session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _login(client, username, password):
    return await client.post("/api/v1/auth/login", json={"username": username, "password": password})


# ---------- 登录 ----------

async def test_login_manager(client):
    r = await _login(client, "manager", "123456")
    assert r.status_code == 200
    b = r.json()
    assert b["username"] == "manager" and b["role"] == "manager" and b["token"]


async def test_login_purchaser(client):
    r = await _login(client, "purchaser", "123456")
    assert r.status_code == 200 and r.json()["role"] == "purchaser"


async def test_login_order_clerk(client):
    r = await _login(client, "order_clerk", "123456")
    assert r.status_code == 200 and r.json()["role"] == "order_clerk"


async def test_login_wrong_password(client):
    r = await _login(client, "manager", "wrong")
    assert r.status_code == 401


# ---------- me ----------

async def test_me_with_token(auth_headers):
    # 用纯 token 验证依赖（无 app 也行：直接构造 FastAPI auth 路由最小实例）
    from app.api.auth import router as auth_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/auth/me", headers=auth_headers("purchaser"))
        assert r.status_code == 200 and r.json()["role"] == "purchaser"


# ---------- approve 权限 ----------

async def test_approve_allows_manager(client, auth_headers):
    # 守卫通过后进入 handler；用不存在的订单应返回 404（而非 401/403）
    r = await client.post("/api/v1/purchase/999999/approve",
                          json={"approved": True}, headers=auth_headers("manager"))
    assert r.status_code == 404


async def test_approve_allows_purchaser(client, auth_headers):
    r = await client.post("/api/v1/purchase/999999/approve",
                          json={"approved": True}, headers=auth_headers("purchaser"))
    assert r.status_code == 404


async def test_approve_denies_order_clerk(client, auth_headers):
    r = await client.post("/api/v1/purchase/999999/approve",
                          json={"approved": True}, headers=auth_headers("order_clerk"))
    assert r.status_code == 403
