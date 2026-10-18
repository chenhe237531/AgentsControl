"""w07_02_approvals: 闸门审批表

Revision ID: w07_02_approvals
Revises: w07_01_ledger
Create Date: 2026-10-18

窗口: W07（issue W07-I03 / I04）

- 四分级 L0/L1/L2/L3；L2/L3 不进队列（应用层约定，表不区分）；
- 日限额超了 → visible_at 顺延（转明日），不是新列；
- 拒绝即记录：decision ∈ {approved, rejected}，reason 可为空但留字段；
- 夜间 L0 静默：held_reason/held_at 记录"待明日确认"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "w07_02_approvals"
down_revision: str | None = "w07_01_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("level", sa.String(4), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("goal_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("visible_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decider", sa.String(128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("held_reason", sa.Text(), nullable=True),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.CheckConstraint("level IN ('L0','L1','L2','L3')", name="ck_approval_level"),
        sa.CheckConstraint("status IN ('pending','decided')", name="ck_approval_status"),
        sa.CheckConstraint(
            "status = 'pending' OR decision IS NOT NULL",
            name="ck_approval_decision_required_when_decided",
        ),
    )
    op.create_index(
        "ix_approvals_queue", "approvals", ["status", "level", "visible_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_queue", table_name="approvals")
    op.drop_table("approvals")
