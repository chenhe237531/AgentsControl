"""w03_01: 任务队列与租约（tasks + task_device_stats）

Revision ID: w03_01_tasks
Revises: w01_03_merge_heads
Create Date: 2026-10-18

窗口: W03

设计要点（为什么这样建表）：
1. **队列就是索引**：`ix_tasks_queue` 是 partial index，只包含 `state='queued'` 的行，
   队列再长也不会把已完成的百万行扫进来；
2. `lease_token` 是栅栏令牌：每次领取重新生成，心跳/交货都带它，
   旧 worker 醒来后无法覆盖新租约；
3. `requires` 用 JSONB 而不是四个布尔列：能力标签会增删（W04/W06 已在读），
   JSONB 让加标签不需要迁移；代价是过滤要用 `->>` 表达式，已在 claim SQL 里固定；
4. `cost_spent` / `budget_cap` 预留在任务行上，是为了派活时**不扫账本**（W07 提供明细）。

降级会 drop 两张表，因此只允许在确认无数据依赖时执行。

挂载点：W01/W02 的迁移链已在 `w01_03_merge_heads` 合流，本迁移接在其后，
保证 `upgrade head` 只有一个 head（不再出现多 head 需要额外 merge 节点）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "w03_01_tasks"
down_revision: str | None = "w01_03_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requires",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reclaim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("budget_cap", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column(
            "cost_spent",
            sa.Numeric(precision=12, scale=4),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("priority BETWEEN 0 AND 9", name="ck_tasks_priority_range"),
        sa.CheckConstraint(
            "state IN ('queued','held','leased','running','done','failed','dead','cancelled')",
            name="ck_tasks_state_enum",
        ),
        sa.CheckConstraint("reclaim_count >= 0", name="ck_tasks_reclaim_count_nonneg"),
        sa.CheckConstraint("retry_count >= 0", name="ck_tasks_retry_count_nonneg"),
        sa.CheckConstraint(
            "budget_cap IS NULL OR budget_cap >= 0", name="ck_tasks_budget_cap_nonneg"
        ),
        sa.CheckConstraint("cost_spent >= 0", name="ck_tasks_cost_spent_nonneg"),
    )
    op.create_index(
        "ix_tasks_queue",
        "tasks",
        ["priority", "queued_at"],
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        "ix_tasks_lease_expiry",
        "tasks",
        ["leased_until"],
        postgresql_where=sa.text("state IN ('leased','running')"),
    )
    op.create_index("ix_tasks_device_state", "tasks", ["device_id", "state"])
    op.create_index("ix_tasks_goal", "tasks", ["goal_id"])
    op.create_index(
        "ix_tasks_retry_due",
        "tasks",
        ["next_retry_at"],
        postgresql_where=sa.text("state = 'failed'"),
    )

    op.create_table(
        "task_device_stats",
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column(
            "consecutive_high", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("device_id"),
    )


def downgrade() -> None:
    op.drop_table("task_device_stats")
    op.drop_index("ix_tasks_retry_due", table_name="tasks")
    op.drop_index("ix_tasks_goal", table_name="tasks")
    op.drop_index("ix_tasks_device_state", table_name="tasks")
    op.drop_index("ix_tasks_lease_expiry", table_name="tasks")
    op.drop_index("ix_tasks_queue", table_name="tasks")
    op.drop_table("tasks")
