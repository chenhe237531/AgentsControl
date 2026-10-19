"""w03_02: 死信表（dead_letter）

Revision ID: w03_02_dead_letter
Revises: w03_01_tasks
Create Date: 2026-10-18

窗口: W03

任务进入 `tasks.state='dead'` 时**必须**同时写一行死信（产品红线：绝不静默丢弃）。
死信 100% 计入账本的「返工」分项（W07 只读本表）。

`resolved_at` / `resolution` 记录人工复活动作：
自动重试永远不会写这两个字段，只有 `POST /tasks/{id}/requeue` 才会。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w03_02_dead_letter"
down_revision: str | None = "w03_01_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("reclaim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_dead_letter_task", "dead_letter", ["task_id"])
    op.create_index(
        "ix_dead_letter_open",
        "dead_letter",
        ["created_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_open", table_name="dead_letter")
    op.drop_index("ix_dead_letter_task", table_name="dead_letter")
    op.drop_table("dead_letter")
