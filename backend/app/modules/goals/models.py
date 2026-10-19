"""goals 模块 ORM 模型。

设计要点（plan/W02.md §2.1 / §2.2）：
- 双轨同表：`track` 区分 money / growth；预算金额用不同列并允许 null；
  - money 轨用 budget_money（¥），growth 轨用 budget_hours（小时）；
- 编号 `O-{M|E}-YYYYMMDD-NNN` 由 service 通过查 max + retry 生成，
  数据库层用 UNIQUE(code) 兜底防止并发冲突；
- 状态机 draft → active → {achieved, frozen, degraded}，
  迁移规则在 `rules.py`，非法迁移返回 409；
- 终态（achieved/frozen/degraded）持时间戳，便于审计与统计。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    owner_subject: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    track: Mapped[str] = mapped_column(String(16), nullable=False)
    template: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    acceptance: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    horizon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    horizon_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    budget_money: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    budget_hours: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    attention_minutes_per_day: Mapped[int | None] = mapped_column(nullable=True)
    north_star: Mapped[str | None] = mapped_column(String(200), nullable=True)
    guardrails: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    deliverables: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", index=True
    )
    intake_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    degraded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_loss_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["Goal"]