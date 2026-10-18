"""身份与权限模块 —— owner: W01

单用户白名单系统，不开放注册。本文件只做 HTTP：解析入参、调 service、格式化响应，
**不写业务逻辑**（规则在 `service.py`，SQL 在 `repository.py`）。

端点：
- `POST /auth/login`    邮箱 + 密码 → 访问令牌（15 min）+ 刷新令牌（14 天）
- `POST /auth/refresh`  刷新并**轮换**刷新令牌（旧 token 立即失效，重放吊销整条会话）
- `POST /auth/logout`   吊销当前会话的刷新令牌（需 `auth:session`，三档角色都有）
- `GET  /auth/me`       当前主体、角色与 scopes（W10 登录后首屏要用）
- `GET  /auth/scopes`   角色 → scope 矩阵（前端据此显隐按钮，不硬编码角色）

权限模型见 `rbac.py`：`require_scope()` 只能从这里导入，scope 字符串不要散落到各模块。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.core.deps import OwnerDep, Principal

from .deps import ClaimsDep, IdentityServiceDep, client_ip_from_headers
from .rbac import ALL_SCOPES, effective_scopes, require_scope, role_matrix
from .schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    ScopesResponse,
    TokenResponse,
)
from .service import TokenPair

MODULE = "identity"
OWNER_WINDOW = "W01"

router = APIRouter(prefix="/auth", tags=["identity"])


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
    payload: LogoutRequest | None = None,
    session_id: Annotated[
        str | None, Query(description="显式指定会话（「吊销全部会话」见 W01-I03）")
    ] = None,
) -> Response:
    await service.logout(
        session_id=session_id or str(claims.get("sid") or "") or None,
        refresh_token=payload.refresh_token if payload else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
