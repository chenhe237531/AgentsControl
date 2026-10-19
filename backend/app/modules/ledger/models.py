"""账本 ORM 模型 —— owner: W07

三张表：
- `ledger_entries`     钱账本（六分项成本），每条必须可追溯到 task_id 或 goal_id；
- `attention_entries`  注意力账本（决策耗时，分钟）；
- `subscriptions`      订阅登记（摊销与配额消耗率的输入）。

纪律（plan/W07.md §2）：
1. 金额用 NUMERIC(14,4)，绝不用 float；
2. 每条 entry 至少挂 task_id / goal_id 之一（CHECK 约束兜底，拒绝无主成本）；
3. 归集幂等靠 UNIQUE(source, source_id)，不靠应用层记忆。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# 六分项（CPO 口径，docs/INTEGRATION-ORCA.md §6.1）：
# 订阅摊销 / 工具订阅 / 设备 / 返工 / 质检 / 人力
CATEGORIES: tuple[str, ...] = (
    "subscription",
    "tools",
    "device",
    "rework",
    "qc",
    "labor",
)

CURRENCY_DEFAULT = "CNY"


class LedgerEntry(Base):
    """一条成本记录。负数允许（冲正/退款），但绝不为 0（0 没有信息量）。"""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_ledger_source"),
        CheckConstraint(
            "task_id IS NOT NULL OR goal_id IS NOT NULL",
            name="ck_ledger_traceable",
        ),
        CheckConstraint(
            "category IN ('subscription','tools','device','rework','qc','labor')",
            name="ck_ledger_category",
        ),
        CheckConstraint("amount <> 0", name="ck_ledger_amount_nonzero"),
        Index("ix_ledger_occurred_at", "occurred_at"),
        Index("ix_ledger_goal_id", "goal_id"),
        Index("ix_ledger_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default=CURRENCY_DEFAULT, server_default=CURRENCY_DEFAULT
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 归集来源：task_event / device_event / subscription / manual / ...
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AttentionEntry(Base):
    """一次注意力消耗（分钟）。来源：闸门操作（gate）、今日页停留（today_page，后续接）。"""

    __tablename__ = "attention_entries"
    __table_args__ = (
        CheckConstraint("minutes > 0", name="ck_attention_minutes_positive"),
        Index("ix_attention_occurred_at", "occurred_at"),
        Index("ix_attention_source", "source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    minutes: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Subscription(Base):
    """订阅登记：边际成本≈0，改看摊销 + 配额消耗率（INTEGRATION-ORCA.md §6.1）。"""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "amortize_mode IN ('calendar','usage')",
            name="ck_subscription_mode",
        ),
        CheckConstraint("monthly_fee > 0", name="ck_subscription_fee_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    monthly_fee: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    quota_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # calendar=按自然月天数均摊；usage=按实际用量摊
    amortize_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="calendar", server_default="calendar"
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
