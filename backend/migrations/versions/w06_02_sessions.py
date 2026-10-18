"""w06: execution_sessions

Revision ID: w06_02_sessions
Revises: w06_01_workers
Create Date: 2026-10-18

窗口: W06（会话与 worktree 生命周期，issue W06-I03）

表语义：会话 = 租约 + worktree。一任务一分支一 worktree，命名
`.worktrees/<issue>-<slug>` / 分支 `issue/<issue>-<slug>`；回收前检查
未提交改动（有改动 → 409 worktree_dirty，防 Agent 工作被静默丢弃）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w06_02_sessions"
down_revision: str | None = "w06_01_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("issue", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False, server_default="generic"),
        sa.Column("state", sa.Text(), nullable=False, server_default="created"),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=True),
        sa.Column("orca_session", sa.Text(), nullable=True),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("objective_id", sa.Text(), nullable=True),
        sa.Column("transcript_url", sa.Text(), nullable=True),
        sa.Column("diff_summary", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fleet_task", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("spawned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_execution_sessions_task_id", "execution_sessions", ["task_id"])
    op.create_index("ix_execution_sessions_device_id", "execution_sessions", ["device_id"])
    op.create_index("ix_execution_sessions_objective_id", "execution_sessions", ["objective_id"])
    op.create_index(
        "ix_execution_sessions_issue_slug", "execution_sessions", ["issue", "slug"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_sessions_issue_slug", table_name="execution_sessions")
    op.drop_index("ix_execution_sessions_objective_id", table_name="execution_sessions")
    op.drop_index("ix_execution_sessions_device_id", table_name="execution_sessions")
    op.drop_index("ix_execution_sessions_task_id", table_name="execution_sessions")
    op.drop_table("execution_sessions")
