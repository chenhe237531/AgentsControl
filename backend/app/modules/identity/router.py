"""身份与权限模块 —— owner: W01

单用户白名单系统，不开放注册。本文件只做 HTTP：解析入参、调 service、格式化响应，
**不写业务逻辑**（规则在 `service.py`，SQL 在 `repository.py`）。

端点：
- `POST /auth/login`    邮箱 + 密码 → 访问令牌（15 min）+ 刷新令牌（14 天）
- `POST /auth/refresh`  刷新并**轮换**刷新令牌（旧 token 立即失效，重放吊销整条会话）
- `POST /auth/logout`   吊销当前会话的刷新令牌（需 `auth:session`，三档角色都有）
- `GET  /auth/sessions` 当前主体的登录会话列表（含已吊销，按最近活动倒序）
- `POST /auth/sessions/revoke-all` 吊销全部会话（默认保留当前，"踢掉其它设备"）
- `GET  /auth/me`       当前主体、角色与 scopes（W10 登录后首屏要用）
- `GET  /auth/scopes`   角色 → scope 矩阵（前端据此显隐按钮，不硬编码角色）

权限模型见 `rbac.py`：`require_scope()` 只能从这里导入，scope 字符串不要散落到各模块。

吊销语义（见 `docs/adr/0003-session-revocation.md`）：吊销**只作用于刷新令牌**，
已签发的访问令牌是无状态 JWT，最多还能再用 15 分钟——这是刻意取舍，不是 bug。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.core.deps import OwnerDep, Principal
from app.core.errors import UnauthorizedError
from app.core.ratelimit import auth_rate_limit

from .deps import ClaimsDep, IdentityServiceDep, client_ip_from_headers
from .rbac import ALL_SCOPES, effective_scopes, require_scope, role_matrix
from .schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    RevokeAllSessionsRequest,
    RevokeAllSessionsResponse,
    ScopesResponse,
    SessionInfo,
    SessionsResponse,
    TokenResponse,
)
from .service import TokenPair

MODULE = "identity"
OWNER_WINDOW = "W01"

router = APIRouter(
    prefix="/auth",
    tags=["identity"],
    # 路由级守卫：`/auth/*` 全部端点在**解析入参之前**过一遍 IP + 主体双维度限流，
    # 这样未认证的爆破请求也会被计数（端点级依赖挂在认证之后，挡不住爆破）。
    # 桶与额度见 `app/core/ratelimit.py` 的规则表，本文件不写数字。
    dependencies=[Depends(auth_rate_limit)],
)


def _token_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.access_expires_in,
        refresh_expires_in=pair.refresh_expires_in,
        session_id=pair.session_id,
        subject=pair.subject,
        role=pair.role,
        scopes=list(pair.scopes),
    )


@router.post("/login", response_model=TokenResponse, summary="登录（失败 5 次/15 分钟限流）")
async def login(
    payload: LoginRequest,
    request: Request,
    service: IdentityServiceDep,
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> TokenResponse:
    pair = await service.login(
        payload.email,
        payload.password,
        client_ip=client_ip_from_headers(
            x_forwarded_for, request.client.host if request.client else None
        ),
        user_agent=user_agent,
    )
    return _token_response(pair)


@router.post("/refresh", response_model=TokenResponse, summary="刷新访问令牌（轮换刷新令牌）")
async def refresh(payload: RefreshRequest, service: IdentityServiceDep) -> TokenResponse:
    return _token_response(await service.refresh(payload.refresh_token))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出（吊销当前会话的刷新令牌）",
)
async def logout(
    # `auth:session` 三档角色都有：登出是自助操作，不该要求管理员权限；
    # 但它是写端点，必须挂 scope（否则审计脚本会把这里标成越权缺口）。
    principal: Annotated[Principal, Depends(require_scope("auth:session"))],
    claims: ClaimsDep,
    service: IdentityServiceDep,
    request: Request,
    payload: LogoutRequest | None = None,
    session_id: Annotated[
        str | None, Query(description="显式指定会话（「吊销全部会话」见 POST /auth/sessions/revoke-all）")
    ] = None,
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> Response:
    await service.logout(
        session_id=session_id or str(claims.get("sid") or "") or None,
        refresh_token=payload.refresh_token if payload else None,
        subject=principal.subject,
        client_ip=client_ip_from_headers(
            x_forwarded_for, request.client.host if request.client else None
        ),
        user_agent=user_agent,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions",
    response_model=SessionsResponse,
    summary="当前主体的登录会话（含已吊销，按最近活动倒序）",
)
async def list_sessions(
    # 与 logout 同一个 scope：看自己的会话不需要管理员权限，但必须有认证。
    principal: Annotated[Principal, Depends(require_scope("auth:session"))],
    claims: ClaimsDep,
    service: IdentityServiceDep,
) -> SessionsResponse:
    now = datetime.now(UTC)
    current_sid = str(claims.get("sid") or "")
    records = await service.list_sessions(principal.subject)
    items = [
        SessionInfo(
            session_id=r.session_id,
            current=bool(current_sid) and r.session_id == current_sid,
            active=r.is_active(now),
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
            revoked_reason=r.revoked_reason,
            client_ip=r.client_ip,
            user_agent=r.user_agent,
            token_count=r.token_count,
        )
        for r in records
    ]
    return SessionsResponse(
        sessions=items,
        total=len(items),
        active=sum(1 for i in items if i.active),
    )


@router.post(
    "/sessions/revoke-all",
    response_model=RevokeAllSessionsResponse,
    summary="吊销全部会话（默认保留当前会话）",
)
async def revoke_all_sessions(
    # 同样是自助操作：只能吊销**自己**的会话。主体取自已验签的令牌，
    # 刻意不接受请求体传 subject——否则任何登录用户都能把别人的会话踢掉。
    principal: Annotated[Principal, Depends(require_scope("auth:session"))],
    claims: ClaimsDep,
    service: IdentityServiceDep,
    request: Request,
    payload: RevokeAllSessionsRequest | None = None,
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> RevokeAllSessionsResponse:
    keep_current = payload.keep_current if payload is not None else True
    current_sid = str(claims.get("sid") or "")
    if keep_current and not current_sid:
        # 宁可报错，也不要「想保留当前会话，结果把自己也踢下线」。
        raise UnauthorizedError("当前令牌缺少会话标识，无法只吊销其它会话", code="invalid_token")

    revoked = await service.revoke_all_sessions(
        principal.subject,
        keep_session_id=current_sid if keep_current else None,
        client_ip=client_ip_from_headers(
            x_forwarded_for, request.client.host if request.client else None
        ),
        user_agent=user_agent,
    )
    return RevokeAllSessionsResponse(
        revoked_sessions=revoked,
        kept_session_id=current_sid if keep_current else None,
    )


@router.get("/me", response_model=MeResponse, summary="当前主体、角色与 scopes")
async def me(principal: OwnerDep, claims: ClaimsDep) -> MeResponse:
    return MeResponse(
        subject=principal.subject,
        role=principal.role,
        scopes=list(principal.scopes),
        email=(claims.get("email") or None),
        session_id=(claims.get("sid") or None),
    )


@router.get("/scopes", response_model=ScopesResponse, summary="角色 → scope 矩阵（前端显隐按钮用）")
async def scopes_matrix(principal: OwnerDep) -> ScopesResponse:
    # owner 的通配符在这里已经展开成具体列表，前端不需要理解 "*" 的语义
    return ScopesResponse(
        role=principal.role,
        scopes=sorted(effective_scopes(principal.role, principal.scopes)),
        roles=role_matrix(),
        all_scopes=list(ALL_SCOPES),
    )


@router.get("/_status", summary="模块状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "implemented"}


@router.get("/_ping", summary="需要认证的探针（验证认证链路）")
async def module_ping(principal: OwnerDep) -> dict[str, str]:
    return {"module": MODULE, "subject": principal.subject}
