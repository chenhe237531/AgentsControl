"""身份模块的 ORM 模型。

三张表：
- `owner_credentials`：单用户系统的唯一登录凭据（不开放注册，只能由运维脚本写入）；
- `refresh_tokens`：刷新令牌。**只存哈希**，且以 `session_id` 串成「会话链」，
  一次重放即整链吊销——这是本模块最重要的安全不变量；
- `auth_events`：认证域的安全事件流水（登录/刷新/重放/登出/会话吊销）。
  只追加不修改；设备侧（W04）吊销 token 时也写这里，于是「谁在什么时候把哪个会话踢掉」
  能在一个地方查全，不需要跨模块拼日志。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OwnerCredential(Base):
    """登录凭据。subject 是稳定身份（令牌里的 `sub`），email 只用于登录。"""

    __tablename__ = "owner_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    """刷新令牌记录。`token_hash` 唯一，旋转时旧行只写 `rotated_at`，不删除（留审计轨迹）。"""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_session", "session_id"),
        Index("ix_refresh_tokens_subject", "subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    parent_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


#: `auth_events.event` 的取值白名单。**新增事件必须先加到这里**，
#: 否则 `service` 层会拒绝写入（宁可写失败，也不要让事件名悄悄漂移成三四种拼法）。
AUTH_EVENT_KINDS: tuple[str, ...] = (
    "login",
    "refresh",
    "refresh_replayed",
    "logout",
    "session_revoked",
    "device_revoked",
)


class AuthEvent(Base):
    """认证事件流水。**只追加**：不改、不删，用于事后回答「这个会话是怎么没的」。

    设备侧（W04）吊销设备令牌时写 `event="device_revoked"` 并带上 `session_id`，
    这样 `GET /auth/sessions` 的 `revoked_reason` 与事件表能对上，排查时不用猜。
    """

    __tablename__ = "auth_events"
    __table_args__ = (
        Index("ix_auth_events_subject_created", "subject", "created_at"),
        Index("ix_auth_events_session", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 同一事件的可解释细节（如 `reason=refresh_replayed`、`revoked=2`），不存任何令牌原文
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
