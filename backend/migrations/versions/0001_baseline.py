"""baseline: schema_meta

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-18

窗口: W01

基线只做两件事：
1. 建立 `schema_meta`，记录 schema 版本与引导时间（便于运维核对）；
2. 打开需要的扩展（`pgcrypto` 用于 gen_random_uuid）。

刻意不在基线里建业务表：业务表由各窗口在自己的 revision 中创建，
避免基线成为所有窗口的冲突点。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "schema_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '0001'), ('bootstrap', 'agent-fleet-control')"
    )


def downgrade() -> None:
    op.drop_table("schema_meta")
