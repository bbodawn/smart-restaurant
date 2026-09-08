"""登录 / 当前用户（Phase 9-A）。mock 用户，返回 HMAC token。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import ALL_ROLES, USERS, create_token, require_roles

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: LoginRequest):
    """mock 登录：校验用户名/密码，签发 token。"""
    user = USERS.get(body.username)
    if user is None or user["password"] != body.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token = create_token(body.username, user["role"])
    return {
        "token": token,
        "username": body.username,
        "role": user["role"],
    }


@router.get("/auth/me")
async def me(auth: dict = Depends(require_roles(ALL_ROLES))):
    """返回当前登录用户信息（任意已登录角色）。"""
    return {
        "username": auth["username"],
        "role": auth["role"],
    }
