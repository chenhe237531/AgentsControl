"""w02_01_intake: 创建 intake_items 表

Revision ID: w02_01_intake
Revises: 0001_baseline
Create Date: 2026-10-18

窗口: W02 · Issue W02-I01

设计要点：
- 单表多状态（raw / structured / promoted / archived_interest），便于统一查询与统计；
- text_hash + owner_subject 用于 5 分钟去重（同一文本短期内不重复入库）；
- keyset 分页：业务层 ORDER BY (created_at DESC, id DESC)；
- structured 用 JSONB，存放 AI 三问答案与原始模型输出；
- 14 天归档为兴趣池走 UPDATE status='archived_interest'，不删除原行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w02_01_intake"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_subject", sa.String(length=128), nullable=False),
        sa.Column("text", sa.String(length=2000), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="raw",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("structured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("structured", sa.JSON(), nullable=True),
        sa.Column("structure_model", sa.String(length=64), nullable=True),
        sa.Column("structure_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_goal_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_intake_items_owner_subject",
        "intake_items",
        ["owner_subject"],
    )
    op.create_index(
        "ix_intake_items_text_hash",
        "intake_items",
        ["text_hash"],
    )
    op.create_index(
        "ix_intake_items_status",
        "intake_items",
        ["status"],
    )
    # 5 分钟去重查询路径：text_hash + owner_subject + created_at
    op.create_index(
        "ix_intake_items_owner_text_created",
        "intake_items",
        ["owner_subject", "text_hash", "created_at"],
    )
    # keyset 分页查询路径：owner + (created_at, id) DESC
    op.create_index(
        "ix_intake_items_owner_created_id",
        "intake_items",
        ["owner_subject", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_intake_items_owner_created_id", table_name="intake_items")
    op.drop_index("ix_intake_items_owner_text_created", table_name="intake_items")
    op.drop_index("ix_intake_items_status", table_name="intake_items")
    op.drop_index("ix_intake_items_text_hash", table_name="intake_items")
    op.drop_index("ix_intake_items_owner_subject", table_name="intake_items")
    op.drop_table("intake_items")
