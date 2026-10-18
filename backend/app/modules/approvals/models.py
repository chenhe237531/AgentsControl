"""闸门（审批）ORM 模型 —— owner: W07

四分级（plan/W07.md §2）：
- L0 战略：必须人批（"这人这么值钱吗"）；
- L1 例外：抽样复核（CI 红了、超时了、连续多轮返工了）；
- L2 常规：机器默认（人工可抽查），不进队列；
- L3 机械：纯物理动作，默认对用户不可见。

限额：L0 ≤2 个/日、L1 ≤8 个/日，超了自动转明日（visible_at 顺延）。
拒绝即记录：rejected 是一等决策，不是删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3")
# 进队列（打扰人）的级别；L2/L3 只在抽检页可见
QUEUE_LEVELS: tuple[str, ...] = ("L0", "L1")
STATUSES: tuple[str, ...] = ("pending", "decided")
DECISIONS: tuple[str, ...] = ("approved", "rejected")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("level IN ('L0','L1','L2','L3')", name="ck_approval_level"),
        CheckConstraint("status IN ('pending','decided')", name="ck_approval_status"),
        CheckConstraint(
            "status = 'pending' OR decision IS NOT NULL",
            name="ck_approval_decision_required_when_decided",
        ),
        Index("ix_approvals_queue", "status", "level", "visible_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    level: Mapped[str] = mapped_column(String(4), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    # 日限额超了 → visible_at 顺延到明日（转明日，不是丢弃）
    visible_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # L0 在 23:00–07:00 被决策 → 只记录"待明日确认"，不给结论
    held_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
