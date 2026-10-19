"""w08_01_evals: 评测题库、运行与结果三张表

Revision ID: w08_01_evals
Revises: w01_03_merge_heads
Create Date: 2026-10-18

窗口: W08 · Issue W08-I01

设计要点：
- 分数 Numeric(5,2)、成本 Numeric(12,6)：评测成本要对账，浮点累加误差不可接受；
- eval_results 的 (run_id, task_id, model) 唯一：一次运行内同题同模型只能有一条，
  重复写入直接报错，而不是静默多算一次钱；
- cache_key 索引支撑 7 天缓存查询（同 model + 同题 + 同题目版本）；
- 题目软删（active=false + archived_at），历史 run 的结果仍可追溯。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w08_01_evals"
down_revision: str | None = "w01_03_merge_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("rubric", sa.JSON(), nullable=False),
        sa.Column("max_score", sa.Numeric(5, 2), nullable=False, server_default="100"),
        sa.Column("weight", sa.Numeric(5, 2), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_eval_tasks_dimension", "eval_tasks", ["dimension"])
    op.create_index("ix_eval_tasks_active", "eval_tasks", ["active"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="fake"),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("task_ids", sa.JSON(), nullable=False),
        sa.Column("cost_cap_cny", sa.Numeric(12, 4), nullable=False, server_default="20"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_eval_runs_status", "eval_runs", ["status"])

    op.create_table(
        "eval_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "run_id", "task_id", "model", name="uq_eval_results_run_task_model"
        ),
    )
    op.create_index("ix_eval_results_run_id", "eval_results", ["run_id"])
    op.create_index("ix_eval_results_task_id", "eval_results", ["task_id"])
    op.create_index("ix_eval_results_model", "eval_results", ["model"])
    op.create_index("ix_eval_results_cache_key", "eval_results", ["cache_key"])


def downgrade() -> None:
    op.drop_index("ix_eval_results_cache_key", table_name="eval_results")
    op.drop_index("ix_eval_results_model", table_name="eval_results")
    op.drop_index("ix_eval_results_task_id", table_name="eval_results")
    op.drop_index("ix_eval_results_run_id", table_name="eval_results")
    op.drop_table("eval_results")

    op.drop_index("ix_eval_runs_status", table_name="eval_runs")
    op.drop_table("eval_runs")

    op.drop_index("ix_eval_tasks_active", table_name="eval_tasks")
    op.drop_index("ix_eval_tasks_dimension", table_name="eval_tasks")
    op.drop_table("eval_tasks")
