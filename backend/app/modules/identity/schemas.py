"""身份模块的 HTTP 契约（请求/响应体）。

字段名一旦发布就不能改（W10 前端与外部集成方都依赖），新增字段一律可选。
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        # 只用正则校验，避免为 EmailStr 引入 email-validator 依赖（单用户系统不值当）
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=10)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="访问令牌有效秒数")
    refresh_expires_in: int = Field(description="刷新令牌有效秒数")
    session_id: str
    subject: str
    role: str
    scopes: list[str] = []


class MeResponse(BaseModel):
    subject: str
    role: str
    scopes: list[str] = []
    email: str | None = None
    session_id: str | None = None


class RoleScopes(BaseModel):
    """一档角色的展开后 scope 列表。"""

    role: str
    scopes: list[str] = []


class ScopesResponse(BaseModel):
    """`GET /auth/scopes`：前端据此做按钮显隐，**不要**在前端硬编码角色判断。"""

    role: str
    scopes: list[str] = Field(default_factory=list, description="当前主体的有效 scopes")
    roles: dict[str, list[str]] = Field(
        default_factory=dict, description="角色 → 展开后 scopes（owner 为全部）"
    )
    all_scopes: list[str] = Field(default_factory=list, description="系统登记的全部 scope")


class SessionInfo(BaseModel):
    """一个登录会话（同一 `session_id` 的令牌链聚合）。**不含任何令牌线索**。"""

    session_id: str
    current: bool = Field(default=False, description="是否是本次请求所用的会话")
    active: bool = Field(description="仍有未吊销且未过期的令牌")
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = Field(
        default=None, description="logout / revoke_all / refresh_replayed / device_revoked"
    )
    client_ip: str | None = None
    user_agent: str | None = None
    token_count: int = 0


class SessionsResponse(BaseModel):
    sessions: list[SessionInfo] = Field(default_factory=list)
    total: int = 0
    active: int = 0


class RevokeAllSessionsRequest(BaseModel):
    keep_current: bool = Field(
        default=True, description="true = 保留当前会话（「踢掉其它设备」），false = 连自己一起登出"
    )


class RevokeAllSessionsResponse(BaseModel):
    revoked_sessions: int = Field(description="被吊销的会话数（不是令牌数）")
    kept_session_id: str | None = None
