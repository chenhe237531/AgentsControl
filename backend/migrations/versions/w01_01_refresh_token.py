"""w01_01: 登录凭据与刷新令牌（轮换 + 会话链吊销）

Revision ID: w01_01_refresh_token
Revises: 0001_baseline
Create Date: 2026-10-18

窗口: W01 · issue: W01-I01

设计要点：
- `refresh_tokens` **只存 sha256 哈希**，不存明文；
- `session_id` 把同一会话的令牌串成链，重放时整链 `revoked_at`；
- 不删除旧令牌（保留 `rotated_at`/`revoked_at` 作为审计轨迹）；
- `downgrade()` 可完整回滚（CI 会跑 upgrade → downgrade → upgrade）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "w01_01_refresh_token"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owner_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("subject", sa.String(length=64), nullable=False, unique=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="owner"),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("jti", sa.String(length=64), nullable=False, unique=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("parent_jti", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_refresh_tokens_session", "refresh_tokens", ["session_id"])
    op.create_index("ix_refresh_tokens_subject", "refresh_tokens", ["subject"])
    # 清理任务只扫「未吊销且已过期」的行，部分索引避免全表扫
    op.create_index(
        "ix_refresh_tokens_expires_at",
        "refresh_tokens",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_subject", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("owner_credentials")
