"""闸门数据访问。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals.models import Approval


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        await self._session.commit()

    async def add(self, approval: Approval) -> Approval:
        self._session.add(approval)
        await self._session.flush()
        await self._session.refresh(approval)
        return approval

    async def get(self, approval_id: uuid.UUID) -> Approval | None:
        return await self._session.get(Approval, approval_id)

    async def count_created_between(
        self, level: str, start: datetime, end: datetime
    ) -> int:
        stmt = select(func.count()).select_from(Approval).where(
            Approval.level == level,
            Approval.created_at >= start,
            Approval.created_at < end,
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def queue(
        self,
        *,
        levels: tuple[str, ...],
        now: datetime,
        limit: int = 50,
    ) -> list[Approval]:
        """可见队列：pending 且 visible_at <= now（被限额顺延的不出现）。"""
        stmt = (
            select(Approval)
            .where(
                Approval.status == "pending",
                Approval.level.in_(levels),
                Approval.visible_at <= now,
            )
            .order_by(Approval.created_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def decision_stats(
        self, start: datetime, end: datetime
    ) -> tuple[int, int]:
        """(approved, rejected) 计数，用于否决率。"""
        stmt = (
            select(Approval.decision, func.count())
            .where(
                Approval.status == "decided",
                Approval.decided_at >= start,
                Approval.decided_at < end,
            )
            .group_by(Approval.decision)
        )
        rows = (await self._session.execute(stmt)).all()
        stats = {str(decision): int(n) for decision, n in rows}
        return stats.get("approved", 0), stats.get("rejected", 0)

    async def decided_today(self, now: datetime) -> int:
        day_start = datetime(
            now.year, now.month, now.day, tzinfo=now.tzinfo
        )
        stmt = (
            select(func.count())
            .select_from(Approval)
            .where(Approval.status == "decided", Approval.decided_at >= day_start)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def decisions_by_day(self, days: int, now: datetime) -> dict[date, int]:
        """最近 N 天每天的已决策数量（带宽快照用）。"""
        since = now - timedelta(days=days)
        stmt = (
            select(
                func.date(Approval.decided_at).label("d"), func.count()
            )
            .where(Approval.status == "decided", Approval.decided_at >= since)
            .group_by(func.date(Approval.decided_at))
        )
        rows = (await self._session.execute(stmt)).all()
        return {day: int(n) for day, n in rows if day is not None}
