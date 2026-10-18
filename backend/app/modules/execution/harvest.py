"""harvest：产物与 trace 归集（六原子动作的收口）。

纪律（plan/W06.md I05 / INTEGRATION-ORCA.md §4.3-4.4、§5）：
- diff 摘要走 git numstat（稳定事实）；transcript 存对象存储，库里只存引用；
- harvest 是长任务（大 diff、大 transcript）：202 受理 + 后台执行，过程落
  task_events（session.harvesting / session.harvested），绝不回压请求线程；
- 无产物 → 判定失败：任务转 failed（语义判定，不重试）；
- 后台任务用自己的 DB 会话（202 返回后请求作用域的会话即关闭）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.db import get_sessionmaker

from .orca import OrcaAdapter, OrcaError
from .protocol import TasksGateway
from .repository import EventStore, SessionRow, SessionStore, SqlEventStore, SqlSessionStore
from .service import SessionNotFoundError, SessionNotRunnableError, stop_lease_keeper
from .worktree import WorktreeManager

logger = logging.getLogger("afc.execution.harvest")

DEFAULT_BASE_REF = "main"  # base_ref 未指定时的 diff 基线（仓库默认分支）

# 后台 harvest 任务注册表：幂等（同会话并发 harvest 只跑一个）
_harvest_tasks: dict[str, asyncio.Task[None]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- 对象存储缝
# COS 上线后换实现即可：key 命名与引用格式（cos://bucket/key）不变，库里只存引用。

@runtime_checkable
class ArtifactStore(Protocol):
    async def put_text(self, key: str, text: str) -> str: ...


class LocalArtifactStore:
    """本地实现：写 FLEET_ARTIFACTS_DIR（缺省 <repo>/artifacts），引用形如 file://…。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    async def put_text(self, key: str, text: str) -> str:
        path = self._root / key
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, text, "utf-8")
        return f"file://{path.resolve()}"


def default_artifacts_root() -> str:
    """生产装配：环境变量优先（core 配置冻结，不越界加项）。"""
    root = os.getenv("FLEET_REPO_ROOT", os.getcwd())
    return os.getenv("FLEET_ARTIFACTS_DIR", os.path.join(root, "artifacts"))


# ---------------------------------------------------------------- diff 摘要

@dataclass(frozen=True, slots=True)
class DiffSummary:
    files_changed: int
    insertions: int
    deletions: int
    binary: int
    dirs: tuple[str, ...]
    files: tuple[str, ...]  # 截断的前 N 个文件，防大 diff 撑爆行记录

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "binary": self.binary,
            "dirs": list(self.dirs),
            "files": list(self.files),
        }


def summarize_numstat(numstat: str, *, max_files: int = 50) -> DiffSummary:
    """解析 `git diff --numstat` 输出：文件数、增删行、涉及目录。纯函数便于单测。"""
    files: list[str] = []
    insertions = deletions = binary = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        raw_add, raw_del, path = parts[0], parts[1], parts[2]
        path = path.split(" => ")[-1].split(" -> ")[-1].strip('"')  # 重命名取目标路径
        if not path:
            continue
        if raw_add == "-" or raw_del == "-":
            binary += 1
        else:
            try:
                insertions += int(raw_add)
                deletions += int(raw_del)
            except ValueError:
                binary += 1
        files.append(path)
    dirs = tuple(sorted({p.split("/", 1)[0] for p in files}))
    return DiffSummary(
        files_changed=len(files),
        insertions=insertions,
        deletions=deletions,
        binary=binary,
        dirs=dirs,
        files=tuple(files[:max_files]),
    )


# ---------------------------------------------------------------- 后台存储装配
# 202 返回后请求作用域的 AsyncSession 即关闭：后台任务必须开自己的会话。

StoresFactory = Callable[[], Any]  # () -> async context manager yielding (SessionStore, EventStore)


def sql_stores_factory() -> Any:
    @asynccontextmanager
    async def _open() -> AsyncIterator[tuple[SessionStore, EventStore]]:
        async with get_sessionmaker()() as db:
            yield SqlSessionStore(db), SqlEventStore(db)

    return _open


# ---------------------------------------------------------------- 编排

class HarvestService:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        events: EventStore,
        tasks: TasksGateway,
        worktrees: WorktreeManager,
        orca: OrcaAdapter,
        artifacts: ArtifactStore,
        open_stores: StoresFactory,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._tasks = tasks
        self._worktrees = worktrees
        self._orca = orca
        self._artifacts = artifacts
        self._open_stores = open_stores

    async def start(self, session_id: str) -> SessionRow:
        """校验 + 置 harvesting + 提交，然后立刻返回（202）；收集在后台执行。"""
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        if row.state in ("created", "recycled"):
            raise SessionNotRunnableError(
                f"会话 {session_id} 状态为 {row.state}，没有可收集的运行痕迹"
            )
        if row.state in ("harvesting", "harvested"):
            raise SessionNotRunnableError(
                f"会话 {session_id} 状态为 {row.state}，不可重复 harvest"
            )
        stop_lease_keeper(session_id)  # 收集即收口：租约 keeper 到此为止
        updated = await self._sessions.update(session_id, state="harvesting")
        await self._events.append(
            task_id=row.task_id,
            kind="session.harvesting",
            session_id=session_id,
            payload={},
        )
        await self._sessions.commit()
        _start_harvest(
            session_id,
            worktrees=self._worktrees,
            orca=self._orca,
            artifacts=self._artifacts,
            tasks=self._tasks,
            open_stores=self._open_stores,
        )
        return updated or row


def _start_harvest(
    session_id: str,
    *,
    worktrees: WorktreeManager,
    orca: OrcaAdapter,
    artifacts: ArtifactStore,
    tasks: TasksGateway,
    open_stores: StoresFactory,
) -> None:
    prior = _harvest_tasks.get(session_id)
    if prior is not None and not prior.done():
        return
    _harvest_tasks[session_id] = asyncio.create_task(
        _run_harvest(
            session_id,
            worktrees=worktrees,
            orca=orca,
            artifacts=artifacts,
            tasks=tasks,
            open_stores=open_stores,
        )
    )


async def _run_harvest(
    session_id: str,
    *,
    worktrees: WorktreeManager,
    orca: OrcaAdapter,
    artifacts: ArtifactStore,
    tasks: TasksGateway,
    open_stores: StoresFactory,
) -> None:
    """后台执行体：重活（numstat、transcript 上传）在这里，请求线程不等它。"""
    try:
        async with open_stores() as (sessions, events):
            row = await sessions.get(session_id)
            if row is None:
                return
            summary = DiffSummary(0, 0, 0, 0, (), ())
            if Path(row.worktree_path).exists():
                numstat = await worktrees.diff_numstat(
                    row.worktree_path, base_ref=row.base_ref or DEFAULT_BASE_REF
                )
                summary = summarize_numstat(numstat)

            transcript_url: str | None = None
            cost_by_model: dict[str, Any] = {}
            if row.orca_session:
                try:
                    data = await orca.harvest(session=row.orca_session)
                except OrcaError as exc:
                    # harvest CLI 失败不等于无产物：diff 还在，记录后继续收
                    logger.warning(
                        "orca harvest 失败，降级为仅收集 diff",
                        extra={"session_id": session_id, "err": str(exc)},
                    )
                    data = {}
                transcript_path = data.get("transcript_path")
                if transcript_path and Path(str(transcript_path)).is_file():
                    text = await asyncio.to_thread(
                        Path(str(transcript_path)).read_text, "utf-8"
                    )
                    transcript_url = await artifacts.put_text(
                        f"transcripts/{row.task_id}/{session_id}.jsonl", text
                    )
                raw_cost = data.get("cost") or {}
                if isinstance(raw_cost, dict):
                    cost_by_model = dict(raw_cost.get("by_model") or {})

            # 无产物 → 判定失败（Orca draft §5）：任务转 failed，不重试
            if summary.files_changed == 0 and transcript_url is None:
                reason = "harvest 无产物：diff 为空且无 transcript"
                await tasks.fail(
                    row.task_id,
                    worker=session_id,
                    error_type="no_artifacts",
                    retryable=False,
                    context={"diff_files": summary.files_changed},
                )
                await tasks.release(row.task_id, worker=session_id)
                await events.append(
                    task_id=row.task_id,
                    kind="session.failed",
                    session_id=session_id,
                    payload={"reason": reason, "retryable": False},
                )
                await sessions.update(
                    session_id, state="failed", error=reason, ended_at=_now()
                )
                await sessions.commit()
                logger.info("harvest 判定失败", extra={"session_id": session_id})
                return

            await sessions.update(
                session_id,
                state="harvested",
                diff_summary=summary.to_dict(),
                transcript_url=transcript_url,
                ended_at=_now(),
            )
            await events.append(
                task_id=row.task_id,
                kind="session.harvested",
                session_id=session_id,
                payload={
                    "diff": summary.to_dict(),
                    "transcript_url": transcript_url,
                },
            )
            for model, amount in sorted(cost_by_model.items()):
                await events.append(
                    task_id=row.task_id,
                    kind="cost",
                    session_id=session_id,
                    payload={"model": model, "amount": amount},
                )
            await sessions.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("harvest 后台任务失败", extra={"session_id": session_id})
        try:
            async with open_stores() as (sessions, events):
                row = await sessions.get(session_id)
                if row is not None and row.state == "harvesting":
                    await sessions.update(
                        session_id,
                        state="failed",
                        error=f"harvest 内部错误：{exc}",
                        ended_at=_now(),
                    )
                    await events.append(
                        task_id=row.task_id,
                        kind="session.failed",
                        session_id=session_id,
                        payload={"reason": "harvest_error", "retryable": True},
                    )
                    await sessions.commit()
        except Exception:  # noqa: BLE001
            logger.exception("harvest 失败收尾也失败了", extra={"session_id": session_id})
