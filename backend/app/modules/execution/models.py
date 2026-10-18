"""ORM 模型 —— 执行面自己的表。

红线：不映射、不写 `tasks` 表（那是 W03 的状态机领土）。
执行面只维护三样自己的东西：
- `execution_workers`   Worker 注册表（谁在领活、能力标签、最后在线）
- `task_events`         任务事件流（claimed/completed/failed/cost/…，供 W07 归集、W10 展示）
- `execution_sessions`  会话与 worktree（W06-I03 起交付，追加在同文件）
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
