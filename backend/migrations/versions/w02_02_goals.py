"""w02_02_goals: 创建 goals 表

Revision ID: w02_02_goals
Revises: w02_01_intake
Create Date: 2026-10-18

窗口: W02 · Issue W02-I03

设计要点：
- 双轨同表：track ∈ {money, growth}，预算金额用可空列分别承载 ¥ 与小时；
- 编号 code UNIQUE，由 service 通过 LIKE 前缀查 max 生成，DB 层兜底防并发冲突；
- 状态机 draft → active → {achieved, frozen, degraded}，迁移规则在 rules.py；
- 终态时间戳与 stop_loss_triggered_at 留空，止损判定与降级时填入。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w02_02_goals"
down_revision: str | None = "w02_01_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code", sa.String(length=24), nullable=False, unique=True),
        sa.Column("owner_subject", sa.String(length=128), nullable=False),
        sa.Column("track", sa.String(length=16), nullable=False),
        sa.Column("template", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("acceptance", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("horizon", sa.String(length=64), nullable=True),
        sa.Column("horizon_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_money", sa.Numeric(12, 2), nullable=True),
        sa.Column("budget_hours", sa.Numeric(8, 2), nullable=True),
        sa.Column("attention_minutes_per_day", sa.Integer(), nullable=True),
        sa.Column("north_star", sa.String(length=200), nullable=True),
        sa.Column("guardrails", sa.JSON(), nullable=True),
        sa.Column("deliverables", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("intake_item_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("degraded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_loss_triggered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_goals_owner_subject", "goals", ["owner_subject"])
    op.create_index("ix_goals_status", "goals", ["status"])
    op.create_index("ix_goals_owner_status", "goals", ["owner_subject", "status"])
    op.create_index("ix_goals_code", "goals", ["code"])
    op.create_index("ix_goals_track_status", "goals", ["track", "status"])


def downgrade() -> None:
    op.drop_index("ix_goals_track_status", table_name="goals")
    op.drop_index("ix_goals_code", table_name="goals")
    op.drop_index("ix_goals_owner_status", table_name="goals")
    op.drop_index("ix_goals_status", table_name="goals")
    op.drop_index("ix_goals_owner_subject", table_name="goals")
    op.drop_table("goals")