"""轻量登录/权限（Phase 9-A，demo 不建库、不用第三方 JWT）。

mock 用户 + HMAC 签名 token（纯标准库）。
- create_token / verify_token：token = base64url(username|role|exp) + "." + hmac签名
- require_roles(allowed)：FastAPI 依赖，校验 Authorization: Bearer <token>
"""
import base64
import hashlib
import hmac
import os
import time
from typing import Dict, List, Optional

from fastapi import Header, HTTPException, status

SECRET = os.getenv("AUTH_SECRET", "demo-secret-phase9")
TOKEN_TTL_SECONDS = 8 * 60 * 60  # 8h

# mock 用户（demo：统一演示密码 123456）
USERS: Dict[str, Dict[str, str]] = {
    "manager":     {"password": "123456", "role": "manager",     "display": "主管"},
    "purchaser":   {"password": "123456", "role": "purchaser",   "display": "采购员"},
    "order_clerk": {"password": "123456", "role": "order_clerk", "display": "点单员"},
}

ALL_ROLES = ["manager", "purchaser", "order_clerk"]


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_token(username: str, role: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发 token：base64url(username|role|exp) + '.' + hmac-sha256."""
    exp = int(time.time()) + ttl
    payload = base64.urlsafe_b64encode(f"{username}|{role}|{exp}".encode("utf-8")).decode("ascii")
    return payload + "." + _sign(payload)


def verify_token(token: str) -> Optional[Dict[str, str]]:
    """校验签名与过期；合法返回 {username, role, exp}，否则 None。"""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        username, role, exp = raw.split("|")
        if int(exp) < int(time.time()):
            return None
        return {"username": username, "role": role, "exp": exp}
    except Exception:
        return None


def require_roles(allowed_roles: List[str]):
    """FastAPI 依赖工厂：校验 Authorization Bearer token 且角色 ∈ allowed_roles。"""
    def _dep(authorization: Optional[str] = Header(default=None, alias="Authorization")):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        token = authorization.split(" ", 1)[1].strip()
        user = verify_token(token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: role not allowed")
        return user
    return _dep
