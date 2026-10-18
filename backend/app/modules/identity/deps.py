"""identity 路由的依赖装配。

放在单独文件里有三个原因：
1. `router.py` 保持「只做 HTTP」，一眼能看完；
2. 测试可以 `app.dependency_overrides[get_identity_service]` 换成内存实现，**不依赖 PG/Redis**；
3. 将来换实现（例如加缓存）只改这一处。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import UnauthorizedError
from app.core.security import TokenError, decode_token

from .repository import SqlIdentityRepository
from .service import IdentityService
from .throttle import RedisThrottle


def get_identity_service(session: Annotated[AsyncSession, Depends(get_db)]) -> IdentityService:
    return IdentityService(SqlIdentityRepository(session), RedisThrottle())


IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]


async def access_claims(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """读取已校验访问令牌的原始 claims（登出需要 `sid`，`/auth/me` 需要 `email`）。

    这里只解码不判权：身份合法性由 `OwnerDep` 负责，本依赖不替代它。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("缺少 Bearer 令牌")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_token(token, expect="access")
    except TokenError as exc:  # 过期/伪造都在 OwnerDep 处已拦，这里只兜底
        raise UnauthorizedError("令牌无效或已过期") from exc


ClaimsDep = Annotated[dict[str, Any], Depends(access_claims)]


def client_ip_from_headers(x_forwarded_for: str | None, fallback: str | None) -> str | None:
    """取最左侧的 X-Forwarded-For（Caddy 反代后这是真实客户端）；没有则用连接地址。"""
    if x_forwarded_for:
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return fallback
