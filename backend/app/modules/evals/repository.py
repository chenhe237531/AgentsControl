"""evals 模块数据访问层。

`EvalRepository` 是 Protocol：service/runner 只依赖它，单测注入内存实现，
因此**所有评测逻辑的测试都不需要真库**（验收 ④ 不出网、不连库）。
`SqlEvalRepository` 是生产实现。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError

from .models import EvalResult, EvalRun, EvalTask
from .schemas import TaskOut


def new_id() -> str:
    return uuid.uuid4().hex


def cache_key(model: str, task_id: str, params_sig: str = "") -> str:
    """7 天缓存的键：同 model + 同题 + 同参数。

    题目内容变更时 `params_sig` 应带上题目版本（由调用方传入 task.updated_at），
    否则改题之后仍然命中旧缓存——这是评测系统里最容易犯的错。
    """
    raw = f"{model}\x1f{task_id}\x1f{params_sig}".encode()
    return hashlib.sha256(raw).hexdigest()


def task_params_sig(task: EvalTask) -> str:
    """题目版本签名：改题即失效缓存。"""
    stamp = task.updated_at.isoformat() if task.updated_at else ""
    return f"{task.dimension}:{task.max_score}:{stamp}"


class EvalRepository(Protocol):
    async def create_task(self, task: EvalTask) -> EvalTask: ...

    async def get_task(self, task_id: str, *, include_archived: bool = False) -> EvalTask: ...

    async def list_tasks(
        self,
        *,
        dimension: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[EvalTask]: ...

    async def count_tasks(self) -> int: ...

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> EvalTask: ...

    async def archive_task(self, task_id: str) -> EvalTask: ...

    async def create_run(self, run: EvalRun) -> EvalRun: ...

    async def get_run(self, run_id: str) -> EvalRun: ...

    async def update_run(self, run_id: str, fields: dict[str, Any]) -> EvalRun: ...

    async def add_results(self, results: list[EvalResult]) -> int: ...

    async def list_results(self, run_id: str) -> list[EvalResult]: ...

    async def find_cached(self, key: str, *, since: datetime) -> EvalResult | None: ...


class SqlEvalRepository:
    """生产实现。写方法自行 commit（与 intake 模块保持一致的分工）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_task(self, task: EvalTask) -> EvalTask:
        self._s.add(task)
        await self._s.commit()
        await self._s.refresh(task)
        return task

    async def get_task(self, task_id: str, *, include_archived: bool = False) -> EvalTask:
        stmt = select(EvalTask).where(EvalTask.id == task_id)
        if not include_archived:
            stmt = stmt.where(EvalTask.archived_at.is_(None))
        task = (await self._s.execute(stmt)).scalar_one_or_none()
        if task is None:
            raise NotFoundError("题目不存在或已归档")
        return task

    async def list_tasks(
        self,
        *,
        dimension: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[EvalTask]:
        stmt = select(EvalTask)
        if not include_archived:
            stmt = stmt.where(EvalTask.archived_at.is_(None))
        if dimension:
            stmt = stmt.where(EvalTask.dimension == dimension)
        stmt = stmt.order_by(EvalTask.dimension, EvalTask.created_at).limit(limit)
        return list((await self._s.execute(stmt)).scalars())

    async def count_tasks(self) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(EvalTask).where(EvalTask.archived_at.is_(None))
        return int((await self._s.execute(stmt)).scalar_one())

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> EvalTask:
        task = await self.get_task(task_id)
        for key, value in fields.items():
            setattr(task, key, value)
        await self._s.commit()
        await self._s.refresh(task)
        return task

    async def archive_task(self, task_id: str) -> EvalTask:
        task = await self.get_task(task_id)
        task.active = False
        task.archived_at = datetime.utcnow()
        await self._s.commit()
        await self._s.refresh(task)
        return task

    async def create_run(self, run: EvalRun) -> EvalRun:
        self._s.add(run)
        await self._s.commit()
        await self._s.refresh(run)
        return run

    async def get_run(self, run_id: str) -> EvalRun:
        run = (
            await self._s.execute(select(EvalRun).where(EvalRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError("评测运行不存在")
        return run

    async def update_run(self, run_id: str, fields: dict[str, Any]) -> EvalRun:
        run = await self.get_run(run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        await self._s.commit()
        await self._s.refresh(run)
        return run

    async def add_results(self, results: list[EvalResult]) -> int:
        if not results:
            return 0
        self._s.add_all(results)
        await self._s.commit()
        return len(results)

    async def list_results(self, run_id: str) -> list[EvalResult]:
        stmt = (
            select(EvalResult)
            .where(EvalResult.run_id == run_id)
            .order_by(EvalResult.task_id, EvalResult.model)
        )
        return list((await self._s.execute(stmt)).scalars())

    async def find_cached(self, key: str, *, since: datetime) -> EvalResult | None:
        stmt = (
            select(EvalResult)
            .where(
                EvalResult.cache_key == key,
                EvalResult.cached.is_(False),
                EvalResult.created_at >= since,
            )
            .order_by(desc(EvalResult.created_at))
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()


def to_task_out(task: EvalTask) -> TaskOut:
    return TaskOut(
        id=task.id,
        dimension=task.dimension,
        title=task.title,
        prompt=task.prompt,
        rubric=list(task.rubric or []),
        max_score=float(task.max_score),
        weight=float(task.weight),
        active=task.active,
        archived_at=task.archived_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


__all__ = [
    "EvalRepository",
    "SqlEvalRepository",
    "cache_key",
    "new_id",
    "task_params_sig",
    "to_task_out",
]
