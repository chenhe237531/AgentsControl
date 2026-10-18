"""w01_02: 认证事件流水 auth_events（会话吊销的可追溯性）

Revision ID: w01_02_auth_events
Revises: w01_01_refresh_token
Create Date: 2026-10-18

窗口: W01 · issue: W01-I03

设计要点：
- **只追加**：认证域发生的事（登录/刷新/重放/登出/会话吊销）写一行，事后可回答
  「这个会话是怎么没的」，不需要跨模块拼日志；
- 不存任何令牌原文或哈希，只存 `session_id` 与可解释的 `detail`；
- 设备侧（W04）吊销设备令牌时也写本表（`event='device_revoked'`），
  于是 `refresh_tokens.revoked_reason` 与事件表能一一对上；
- `event` 的取值白名单在 `app/modules/identity/models.py:AUTH_EVENT_KINDS`，
  刻意**不加** DB CHECK 约束：事件种类会随需求增长，约束会让每次新增都要迁移一次。
- `downgrade()` 可完整回滚（CI 会跑 upgrade → downgrade → upgrade）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "w01_02_auth_events"
down_revision: str | None = "w01_01_refresh_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # 主体维度倒序查（"我最近发生过什么"）+ 会话维度查（"这条会话是怎么没的"）
    op.create_index("ix_auth_events_subject_created", "auth_events", ["subject", "created_at"])
    op.create_index("ix_auth_events_session", "auth_events", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_events_session", table_name="auth_events")
    op.drop_index("ix_auth_events_subject_created", table_name="auth_events")
    op.drop_table("auth_events")
