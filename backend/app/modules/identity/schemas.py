"""身份模块的 HTTP 契约（请求/响应体）。

字段名一旦发布就不能改（W10 前端与外部集成方都依赖），新增字段一律可选。
"""

from __future__ import annotations

import re

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
