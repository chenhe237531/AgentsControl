"""ORM 模型 —— 执行面自己的表。

红线：不映射、不写 `tasks` 表（那是 W03 的状态机领土）。
执行面只维护三样自己的东西：
- `execution_workers`   Worker 注册表（谁在领活、能力标签、最后在线）
- `task_events`         任务事件流（claimed/completed/failed/cost/…，供 W07 归集、W10 展示）
- `execution_sessions`  会话 = 租约 + worktree（一任务一分支一 worktree）
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ExecutionWorker(Base):
    __tablename__ = "execution_workers"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    capabilities: Mapped[list] = mapped_column(sa.JSON, nullable=False, server_default=sa.text("'[]'"))
    machine: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    slots: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="active")
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class TaskEvent(Base):
    """任务事件流：W06 是唯一写入方，W07 只读归集，W10 只读展示。"""

    __tablename__ = "task_events"
    __table_args__ = (sa.Index("ix_task_events_task_id_id", "task_id", "id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True, index=True)
    worker_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="service")
    payload: Mapped[dict] = mapped_column(sa.JSON, nullable=False, server_default=sa.text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class ExecutionSession(Base):
    """会话 = 租约 + worktree。状态机：

    created → running（spawn）→ harvesting → harvested
              └→ killed / failed（kill 或会话崩溃）→ harvesting → harvested
    任意状态 → recycled（worktree 已删；创建/回收幂等防重派）
    """

    __tablename__ = "execution_sessions"
    __table_args__ = (sa.Index("ix_execution_sessions_issue_slug", "issue", "slug"),)

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    issue: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    runtime: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="generic")
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="created")
    branch: Mapped[str] = mapped_column(sa.Text, nullable=False)
    worktree_path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_ref: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    orca_session: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    device_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True, index=True)
    objective_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True, index=True)
    transcript_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    diff_summary: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    fleet_task: Mapped[dict] = mapped_column(sa.JSON, nullable=False, server_default=sa.text("'{}'"))
    spawned_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
