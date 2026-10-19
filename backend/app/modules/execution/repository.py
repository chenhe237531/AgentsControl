"""数据访问层。

约定（与模块骨架注释一致）：
- repository 只接受/返回领域对象（dataclass）或行记录，**不感知 HTTP**；
- 写操作后 `flush()`，由 service 层在多步写入完成后调用 `commit()`；
- 事件表 `task_events` 是 W06 的写入领地，W07/W10 只读。

为了在无数据库的环境（并行开发期、CI 单测）下可测，存储以 Protocol 暴露给
service，生产挂 SQLAlchemy 实现，测试挂内存实现（见 tests/test_execution.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from . import models

# ---------------------------------------------------------------- 领域对象

@dataclass(frozen=True, slots=True)
class WorkerInfo:
    id: str
    last_seen_at: datetime
    capabilities: tuple[str, ...] = ()
    slots: int = 1
    status: str = "active"
    machine: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    task_id: str
    kind: str
    id: int | None = None
    session_id: str | None = None
    worker_id: str | None = None
    source: str = "service"
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionRow:
    """会话领域对象。字段与 execution_sessions 表一一对应。"""

    id: str
    task_id: str
    issue: str
    slug: str
    branch: str
    worktree_path: str
    fleet_task: dict[str, Any] = field(default_factory=dict)
    runtime: str = "generic"
    state: str = "created"
    base_ref: str | None = None
    orca_session: str | None = None
    device_id: str | None = None
    objective_id: str | None = None
    transcript_url: str | None = None
    diff_summary: dict[str, Any] | None = None
    error: str | None = None
    spawned_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------- 存储协议

@runtime_checkable
class WorkerStore(Protocol):
    async def upsert(self, info: WorkerInfo) -> WorkerInfo: ...
    async def touch(self, worker_id: str) -> None: ...
    async def get(self, worker_id: str) -> WorkerInfo | None: ...
    async def commit(self) -> None: ...


@runtime_checkable
class EventStore(Protocol):
    async def append(
        self,
        *,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        worker_id: str | None = None,
        source: str = "service",
    ) -> EventRecord: ...
    async def list_for_task(self, task_id: str, *, limit: int = 20) -> list[EventRecord]: ...
    async def list_for_session(self, session_id: str, *, limit: int = 20) -> list[EventRecord]: ...
    async def commit(self) -> None: ...


@runtime_checkable
class SessionStore(Protocol):
    async def create(self, row: SessionRow) -> SessionRow: ...
    async def get(self, session_id: str) -> SessionRow | None: ...
    async def find_active_by_name(self, issue: str, slug: str) -> SessionRow | None: ...
    async def list_sessions(
        self,
        *,
        state: str | None = None,
        device_id: str | None = None,
        objective_id: str | None = None,
        runtime: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRow]: ...
    async def update(self, session_id: str, **fields: Any) -> SessionRow | None: ...
    async def commit(self) -> None: ...


# ---------------------------------------------------------------- SQLAlchemy 实现

def _worker_info(row: models.ExecutionWorker) -> WorkerInfo:
    return WorkerInfo(
        id=row.id,
        capabilities=tuple(row.capabilities or ()),
        slots=row.slots,
        status=row.status,
        machine=row.machine,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
    )


class SqlWorkerStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, info: WorkerInfo) -> WorkerInfo:
        row = await self._session.get(models.ExecutionWorker, info.id)
        if row is None:
            row = models.ExecutionWorker(id=info.id)
            self._session.add(row)
        row.capabilities = list(info.capabilities)
        row.slots = info.slots
        row.status = info.status
        row.machine = info.machine
        row.last_seen_at = info.last_seen_at
        row.updated_at = info.last_seen_at
        await self._session.flush()
        return _worker_info(row)

    async def touch(self, worker_id: str) -> None:
        """刷新 last_seen；未知 worker 顺手登记（heartbeat 允许先于 claim 出现）。"""
        row = await self._session.get(models.ExecutionWorker, worker_id)
        now = datetime.now(tz=row.last_seen_at.tzinfo) if row else datetime.now(UTC)
        if row is None:
            self._session.add(
                models.ExecutionWorker(id=worker_id, last_seen_at=now, updated_at=now)
            )
        else:
            row.last_seen_at = now
            row.updated_at = now
        await self._session.flush()

    async def get(self, worker_id: str) -> WorkerInfo | None:
        row = await self._session.get(models.ExecutionWorker, worker_id)
        return None if row is None else _worker_info(row)

    async def commit(self) -> None:
        await self._session.commit()


def _event_record(row: models.TaskEvent) -> EventRecord:
    return EventRecord(
        id=row.id,
        task_id=row.task_id,
        kind=row.kind,
        session_id=row.session_id,
        worker_id=row.worker_id,
        source=row.source,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )


class SqlEventStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        worker_id: str | None = None,
        source: str = "service",
    ) -> EventRecord:
        row = models.TaskEvent(
            task_id=task_id,
            kind=kind,
            payload=payload,
            session_id=session_id,
            worker_id=worker_id,
            source=source,
        )
        self._session.add(row)
        await self._session.flush()
        return _event_record(row)

    async def list_for_task(self, task_id: str, *, limit: int = 20) -> list[EventRecord]:
        stmt = (
            sa.select(models.TaskEvent)
            .where(models.TaskEvent.task_id == task_id)
            .order_by(models.TaskEvent.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_event_record(r) for r in rows]

    async def list_for_session(self, session_id: str, *, limit: int = 20) -> list[EventRecord]:
        stmt = (
            sa.select(models.TaskEvent)
            .where(models.TaskEvent.session_id == session_id)
            .order_by(models.TaskEvent.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_event_record(r) for r in rows]

    async def commit(self) -> None:
        await self._session.commit()

# ---------------------------------------------------------------- 会话存储（SQL 实现）

_SESSION_COLUMNS = (
    "id", "task_id", "issue", "slug", "runtime", "state", "branch", "worktree_path",
    "base_ref", "orca_session", "device_id", "objective_id", "transcript_url",
    "diff_summary", "error", "fleet_task", "spawned_at", "ended_at",
    "created_at", "updated_at",
)


def _session_row(row: models.ExecutionSession) -> SessionRow:
    return SessionRow(
        id=row.id,
        task_id=row.task_id,
        issue=row.issue,
        slug=row.slug,
        runtime=row.runtime,
        state=row.state,
        branch=row.branch,
        worktree_path=row.worktree_path,
        base_ref=row.base_ref,
        orca_session=row.orca_session,
        device_id=row.device_id,
        objective_id=row.objective_id,
        transcript_url=row.transcript_url,
        diff_summary=row.diff_summary,
        error=row.error,
        fleet_task=dict(row.fleet_task or {}),
        spawned_at=row.spawned_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlSessionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, row: SessionRow) -> SessionRow:
        self._session.add(models.ExecutionSession(**{c: getattr(row, c) for c in _SESSION_COLUMNS}))
        await self._session.flush()
        return row

    async def get(self, session_id: str) -> SessionRow | None:
        row = await self._session.get(models.ExecutionSession, session_id)
        return None if row is None else _session_row(row)

    async def find_active_by_name(self, issue: str, slug: str) -> SessionRow | None:
        stmt = (
            sa.select(models.ExecutionSession)
            .where(
                models.ExecutionSession.issue == issue,
                models.ExecutionSession.slug == slug,
                models.ExecutionSession.state != "recycled",
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return None if row is None else _session_row(row)

    async def list_sessions(
        self,
        *,
        state: str | None = None,
        device_id: str | None = None,
        objective_id: str | None = None,
        runtime: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRow]:
        """会话列表（W06-I06）：单条带索引的查询，给 <150ms 预算留足余量。"""
        stmt = sa.select(models.ExecutionSession)
        if state:
            stmt = stmt.where(models.ExecutionSession.state == state)
        if device_id:
            stmt = stmt.where(models.ExecutionSession.device_id == device_id)
        if objective_id:
            stmt = stmt.where(models.ExecutionSession.objective_id == objective_id)
        if runtime:
            stmt = stmt.where(models.ExecutionSession.runtime == runtime)
        stmt = (
            stmt.order_by(models.ExecutionSession.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_session_row(r) for r in rows]

    async def update(self, session_id: str, **fields: Any) -> SessionRow | None:
        row = await self._session.get(models.ExecutionSession, session_id)
        if row is None:
            return None
        for key, value in fields.items():
            if key == "updated_at":
                continue  # 交给下面的统一刷新
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _session_row(row)

    async def commit(self) -> None:
        await self._session.commit()
