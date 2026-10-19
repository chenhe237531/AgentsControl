"""任务队列 ORM 模型（表：`tasks` / `dead_letter` / `task_device_stats`）。

字段契约（plan/W03.md §5，改动需通知 W02/W04/W06/W07）：
- `goal_id`：目标归属，可为空；无目标的任务不进入账本统计（W02 读）
- `requires` (JSONB)：`{gui, orca, browser, always_on}`，调度唯一依据（W04/W06 读）
- `budget_cap` / `cost_spent`：预算阀门输入；`cost_spent` **只由 W06/W07 通过
  `service.record_cost()` 写**，本窗口的 HTTP 接口只读（避免两套记账）
- `dead_letter`：死信 100% 进入账本「返工」分项（W07 读）

刻意不建外键到 `devices` / `goals`：那两张表属于别的窗口，跨模块外键会让迁移顺序
变成全局串行依赖。`device_id` / `goal_id` 只存值，语义由各自模块保证。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    goal_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)

    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    requires: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    priority: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False, server_default=sa.text("5"))
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default=sa.text("'queued'"))

    # --- 租约 ---
    device_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    #: 栅栏令牌：每次领取重新生成；心跳/交货/失败都必须带上，防止旧 worker 覆盖新租约
    lease_token: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # --- 回收与重试 ---
    reclaim_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    retry_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    max_retries: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("3"))
    retryable: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.text("true"))
    next_retry_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    # --- 预算（W06/W07 记账，本窗口只读）---
    budget_cap: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 4), nullable=True)
    cost_spent: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
    )
    hold_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    # --- 结果与时间线 ---
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint("priority BETWEEN 0 AND 9", name="ck_tasks_priority_range"),
        sa.CheckConstraint(
            "state IN ('queued','held','leased','running','done','failed','dead','cancelled')",
            name="ck_tasks_state_enum",
        ),
        sa.CheckConstraint("reclaim_count >= 0", name="ck_tasks_reclaim_count_nonneg"),
        sa.CheckConstraint("retry_count >= 0", name="ck_tasks_retry_count_nonneg"),
        sa.CheckConstraint("budget_cap IS NULL OR budget_cap >= 0", name="ck_tasks_budget_cap_nonneg"),
        sa.CheckConstraint("cost_spent >= 0", name="ck_tasks_cost_spent_nonneg"),
        # 队列扫描：只命中 queued 行，按「优先级升序 + 先来先服务」
        sa.Index(
            "ix_tasks_queue",
            "priority",
            "queued_at",
            postgresql_where=sa.text("state = 'queued'"),
        ),
        # 回收扫描：租约过期的持有者
        sa.Index(
            "ix_tasks_lease_expiry",
            "leased_until",
            postgresql_where=sa.text("state IN ('leased','running')"),
        ),
        sa.Index("ix_tasks_device_state", "device_id", "state"),
        sa.Index("ix_tasks_goal", "goal_id"),
        sa.Index("ix_tasks_retry_due", "next_retry_at", postgresql_where=sa.text("state = 'failed'")),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Task {self.id} {self.state} p{self.priority}>"


class DeadLetter(Base):
    """死信记录。任务进 `dead` 时**必须**同时写一行，绝不静默丢弃（产品红线）。"""

    __tablename__ = "dead_letter"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    #: 机器可读原因：non_retryable / max_retries_exhausted / lease_reclaimed
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    reclaim_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    retry_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    __table_args__ = (
        sa.Index("ix_dead_letter_task", "task_id"),
        sa.Index(
            "ix_dead_letter_open",
            "created_at",
            postgresql_where=sa.text("resolved_at IS NULL"),
        ),
    )


class TaskDeviceStats(Base):
    """公平调度的最小状态：某设备连续领了几个高优任务。

    不存历史明细（那是 `tasks` 的事），只存一个计数器，领取时读写各一次。
    """

    __tablename__ = "task_device_stats"

    device_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    consecutive_high: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=sa.text("0"))
    last_claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
