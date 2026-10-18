"""业务编排：Worker 协议生命周期 + 会话（= 租约 + worktree）生命周期。

分层纪律：
- 不感知 Request/Response；
- 任务状态只通过 TasksGateway（tasks.service 公开函数）流转，本模块不写 tasks 表；
- 每次「对外可见的语义变化」都落一条 task_events（claimed/completed/failed/session.*），
  heartbeat 仅在携带 progress/step 时落事件（避免事件表被纯续约灌爆）；
- 会话红线：一任务一分支一 worktree；回收前查未提交改动，拒绝即给文件清单，
  Agent 的工作不允许被静默丢弃。
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import ConflictError, NotFoundError, UpstreamError

from .orca import (
    OrcaAdapter,
    OrcaCommandError,
    OrcaError,
    OrcaFormatError,
    OrcaSessionGone,
    get_orca_status,
)
from .protocol import (
    CostBreakdown,
    TasksGateway,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerCompleteResponse,
    WorkerFailRequest,
    WorkerFailResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
)
from .repository import EventStore, SessionRow, SessionStore, WorkerInfo, WorkerStore
from .schemas import (
    SessionCreateRequest,
    SessionKillRequest,
    SessionSendRequest,
    SessionSpawnRequest,
    SessionSteerRequest,
)
from .worktree import WorktreeExistsError, WorktreeManager, fleet_task_idem_key


def _now() -> datetime:
    return datetime.now(UTC)


class SessionExistsError(ConflictError):
    code = "session_exists"
    message = "会话已存在：一任务一分支一 worktree，重派请复用或先回收"


class SessionNotFoundError(NotFoundError):
    code = "session_not_found"
    message = "会话不存在"


class WorktreeDirtyError(ConflictError):
    code = "worktree_dirty"
    message = "worktree 有未提交改动，拒绝回收"


class SessionNotRunnableError(ConflictError):
    code = "session_not_runnable"
    message = "会话当前状态不允许该操作"


class OrcaUnavailableError(ConflictError):
    code = "orca_unavailable"
    message = "Orca 探测不可用，拒绝派 Orca 会话（不猜）"


class SessionCrashedError(ConflictError):
    code = "session_crashed"
    message = "会话已崩溃：任务已转 failed（可重试）且租约已释放"


class OrcaCommandFailedError(UpstreamError):
    code = "orca_command_failed"
    message = "Orca CLI 命令失败"


# ---------------------------------------------------------------- 会话即租约（I04）
# 会话存活期间由后台 keeper 周期续约，请求线程不承担长任务；
# kill/崩溃/回收时统一走 stop_lease_keeper。

LEASE_RENEW_SECONDS = 60.0

_lease_keepers: dict[str, asyncio.Task[None]] = {}


def stop_lease_keeper(session_id: str) -> None:
    task = _lease_keepers.pop(session_id, None)
    if task is not None:
        task.cancel()


def _start_lease_keeper(session_id: str, task_id: str, tasks: TasksGateway) -> None:
    stop_lease_keeper(session_id)
    _lease_keepers[session_id] = asyncio.create_task(
        _lease_keep_loop(session_id, task_id, tasks)
    )


async def _lease_keep_loop(session_id: str, task_id: str, tasks: TasksGateway) -> None:
    while True:
        await asyncio.sleep(LEASE_RENEW_SECONDS)
        await tasks.renew_lease(task_id, worker=session_id)


class WorkerService:
    def __init__(self, *, workers: WorkerStore, events: EventStore, tasks: TasksGateway) -> None:
        self._workers = workers
        self._events = events
        self._tasks = tasks

    # ------------------------------------------------------------ claim

    async def claim(self, req: WorkerClaimRequest) -> WorkerClaimResponse | None:
        """领活：注册/刷新 Worker → 调 tasks.service.lease → 返回任务或 None（HTTP 204）。"""
        worker_id = req.worker_id or f"w-{secrets.token_hex(6)}"
        now = _now()
        await self._workers.upsert(
            WorkerInfo(
                id=worker_id,
                capabilities=tuple(req.capabilities),
                slots=req.concurrency_slots,
                status="active",
                machine=req.machine,
                last_seen_at=now,
                created_at=now,
            )
        )
        leased = await self._tasks.lease(
            capabilities=list(req.capabilities),
            slots=req.concurrency_slots,
            machine=req.machine,
        )
        if leased is None:
            await self._workers.commit()
            return None
        await self._events.append(
            task_id=leased.task_id,
            kind="claimed",
            worker_id=worker_id,
            payload={
                "capabilities": list(req.capabilities),
                "machine": req.machine,
                "slots": req.concurrency_slots,
                "lease_expires_at": leased.lease_expires_at.isoformat(),
            },
        )
        await self._workers.commit()
        return WorkerClaimResponse(
            worker_id=worker_id,
            task={"task_id": leased.task_id, "payload": leased.payload},
            lease_expires_at=leased.lease_expires_at,
        )

    # ------------------------------------------------------------ heartbeat

    async def heartbeat(self, worker_id: str, req: WorkerHeartbeatRequest) -> WorkerHeartbeatResponse:
        """续约：租约新到期时间以 tasks 模块为准。

        纯续约不落事件（防止事件表被 30s 一条的心跳灌爆）；
        带 progress/step 时才落一条 heartbeat，供会话视图展示当前步骤。
        """
        expires = await self._tasks.renew_lease(req.task_id, worker=worker_id)
        if req.progress is not None or req.step is not None:
            await self._events.append(
                task_id=req.task_id,
                kind="heartbeat",
                worker_id=worker_id,
                payload={"progress": req.progress, "step": req.step},
            )
        await self._workers.commit()
        return WorkerHeartbeatResponse(task_id=req.task_id, lease_expires_at=expires)

    # ------------------------------------------------------------ complete

    async def complete(self, worker_id: str, req: WorkerCompleteRequest) -> WorkerCompleteResponse:
        """交货：result 必带产物引用与成本；写 completed 事件（W07 的归集入口）。"""
        result = req.result
        await self._tasks.complete(
            req.task_id, worker=worker_id, result=result.model_dump(mode="json")
        )
        await self._events.append(
            task_id=req.task_id,
            kind="completed",
            worker_id=worker_id,
            payload={
                "artifacts": [a.model_dump(mode="json") for a in result.artifacts],
                "cost": result.cost.model_dump(mode="json"),
                "elapsed_ms": result.elapsed_ms,
            },
        )
        written = 1
        written += await self._append_cost_events(req.task_id, worker_id, result.cost)
        await self._workers.commit()
        return WorkerCompleteResponse(task_id=req.task_id, status="completed", events_written=written)

    # ------------------------------------------------------------ fail

    async def fail(self, worker_id: str, req: WorkerFailRequest) -> WorkerFailResponse:
        """报错：retryable 决定任务回队还是进死信（由 tasks 模块状态机裁决）。"""
        await self._tasks.fail(
            req.task_id,
            worker=worker_id,
            error_type=req.error_type,
            retryable=req.retryable,
            context=req.context,
        )
        await self._events.append(
            task_id=req.task_id,
            kind="failed",
            worker_id=worker_id,
            payload={
                "error_type": req.error_type,
                "retryable": req.retryable,
                "context": req.context or {},
            },
        )
        await self._workers.commit()
        return WorkerFailResponse(task_id=req.task_id, status="failed", retryable=req.retryable)

    # ------------------------------------------------------------ 内部

    async def _append_cost_events(self, task_id: str, worker_id: str, cost: CostBreakdown) -> int:
        """成本按模型分项落事件（plan/W06.md I05 验收 ③ 的口径，complete 时一并写入）。"""
        for model, amount in sorted(cost.by_model.items()):
            await self._events.append(
                task_id=task_id,
                kind="cost",
                worker_id=worker_id,
                payload={"model": model, "amount": amount},
            )
        return len(cost.by_model)


class SessionService:
    """会话 = 租约 + worktree。I03 生命周期，I04 控制面四动作（spawn/send/steer/kill）。"""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        events: EventStore,
        worktrees: WorktreeManager,
        tasks: TasksGateway,
        orca: OrcaAdapter,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._worktrees = worktrees
        self._tasks = tasks
        self._orca = orca

    # ------------------------------------------------------------ 创建（I03）

    async def create_session(self, req: SessionCreateRequest) -> SessionRow:
        """创建会话：查重 → 建 worktree → 写 .fleet-task.json → 落库 + 事件。

        幂等防重派：同 issue+slug 存在未回收会话，或同名 worktree 目录已存在，
        都拒绝（409），绝不产生第二个 worktree。
        """
        slug = req.slug or req.task_id
        existing = await self._sessions.find_active_by_name(req.issue, slug)
        if existing is not None:
            raise SessionExistsError(
                f"会话 {existing.id} 已占用 issue/{req.issue}-{slug}（state={existing.state}）"
            )
        session_id = f"s-{secrets.token_hex(6)}"
        wt_path = str(self._worktrees.worktrees_dir / f"{req.issue}-{slug}")
        fleet_task: dict = {
            "task_id": req.task_id,
            "session_id": session_id,
            "objective_id": req.objective_id,
            "capability": req.capability,
            "budget_remaining": req.budget_remaining or {},
            "acceptance": req.acceptance.model_dump() if req.acceptance else None,
            "prompt_ver": req.prompt_ver,
            "skill_versions": req.skill_versions or {},
            "idem_key": fleet_task_idem_key(req.task_id, wt_path),
            "created_at": _now().isoformat(),
        }
        try:
            info = await self._worktrees.create(
                issue=req.issue, slug=slug, base_ref=req.base_ref, fleet_task=fleet_task
            )
        except WorktreeExistsError as exc:
            raise SessionExistsError(str(exc)) from exc
        row = SessionRow(
            id=session_id,
            task_id=req.task_id,
            issue=req.issue,
            slug=slug,
            runtime=req.runtime,
            state="created",
            branch=info.branch,
            worktree_path=info.path,
            base_ref=info.base_ref,
            device_id=req.device_id,
            objective_id=req.objective_id,
            fleet_task=fleet_task,
            created_at=_now(),
            updated_at=_now(),
        )
        created = await self._sessions.create(row)
        await self._events.append(
            task_id=req.task_id,
            kind="session.created",
            session_id=session_id,
            payload={
                "branch": info.branch,
                "worktree_path": info.path,
                "runtime": req.runtime,
                "idem_key": fleet_task["idem_key"],
            },
        )
        await self._sessions.commit()
        return created

    # ------------------------------------------------------------ 回收（I03）

    async def recycle_session(self, session_id: str, *, force: bool = False) -> SessionRow:
        """回收 worktree。未提交改动 → 409 + 文件清单；force=1 才允许丢弃。

        幂等：已回收的会话直接返回；磁盘上 worktree 目录已消失也照常标记回收。
        """
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        if row.state == "recycled":
            return row
        if row.state == "running":
            raise SessionNotRunnableError(
                f"会话 {session_id} 仍在 running，先 kill 再回收（避免撕毁运行中的会话）"
            )
        if Path(row.worktree_path).exists():
            files = await self._worktrees.uncommitted_files(row.worktree_path)
            if files and not force:
                raise WorktreeDirtyError(
                    "worktree 有未提交改动，拒绝回收（?force=1 可强制丢弃）",
                    details={"files": files[:50]},
                )
            await self._worktrees.remove(row.worktree_path, force=force)
        updated = await self._sessions.update(session_id, state="recycled", ended_at=_now())
        await self._events.append(
            task_id=row.task_id,
            kind="session.recycled",
            session_id=session_id,
            payload={"force": force},
        )
        await self._sessions.commit()
        return updated or row

    # ------------------------------------------------------------ 控制面四动作（I04）

    async def spawn(self, session_id: str, req: SessionSpawnRequest) -> SessionRow:
        """启动会话：probe 可用才派；成功即转 running 并挂上租约 keeper。

        会话即租约：spawn 后 keeper 周期续约，长任务不回压请求线程——
        过程细节一律靠事件表（session.spawned/message/steer/killed/failed）。
        """
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        if row.state != "created":
            raise SessionNotRunnableError(
                f"会话 {session_id} 状态为 {row.state}，只有 created 状态可 spawn"
            )
        if row.runtime != "orca":
            raise SessionNotRunnableError(
                "runtime=generic 会话由 Worker 协议驱动，不走控制面 spawn"
            )
        status = await get_orca_status()
        if not status.available:
            raise OrcaUnavailableError(
                f"Orca 探测不可用（{status.error}），拒绝派 Orca 会话，不猜"
            )
        try:
            orca_session = await self._orca.spawn(
                worktree=row.worktree_path,
                task_id=row.task_id,
                prompt=req.prompt,
                snapshot=req.snapshot,
            )
        except (OrcaCommandError, OrcaFormatError) as exc:
            raise OrcaCommandFailedError(str(exc)) from exc
        except OrcaError as exc:
            raise OrcaUnavailableError(str(exc)) from exc
        updated = await self._sessions.update(
            session_id, state="running", orca_session=orca_session, spawned_at=_now()
        )
        await self._events.append(
            task_id=row.task_id,
            kind="session.spawned",
            session_id=session_id,
            payload={
                "orca_session": orca_session,
                "has_prompt": req.prompt is not None,
                "snapshot": req.snapshot,
            },
        )
        _start_lease_keeper(session_id, row.task_id, self._tasks)
        await self._sessions.commit()
        return updated or row

    async def _control_target(self, session_id: str) -> SessionRow:
        """send/steer 的前置条件：只有 running 的 Orca 会话可接收指令。"""
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        if row.state != "running" or not row.orca_session:
            raise SessionNotRunnableError(
                f"会话 {session_id} 状态为 {row.state}，只有 running 会话可接收指令"
            )
        return row

    async def _run_control(self, row: SessionRow, coro) -> SessionRow:
        """执行 adapter 指令并翻译异常：会话消亡 → 崩溃路径；其他失败 → 502。"""
        try:
            await coro
        except OrcaSessionGone as exc:
            await self._mark_crashed(row, reason=str(exc))
            raise SessionCrashedError(
                f"会话 {row.id} 已崩溃：任务 {row.task_id} 已转 failed（retryable）且租约已释放"
            ) from exc
        except OrcaError as exc:
            raise OrcaCommandFailedError(str(exc)) from exc
        return row

    async def send(self, session_id: str, req: SessionSendRequest) -> SessionRow:
        row = await self._control_target(session_id)
        row = await self._run_control(
            row,
            self._orca.send(session=row.orca_session, prompt=req.prompt, files=req.files),
        )
        await self._events.append(
            task_id=row.task_id,
            kind="session.message",
            session_id=session_id,
            payload={"chars": len(req.prompt), "files": list(req.files)},
        )
        await self._sessions.commit()
        return row

    async def steer(self, session_id: str, req: SessionSteerRequest) -> SessionRow:
        """人工闸门/纠偏：只做生命周期级干预，绝不 inside-the-loop 指挥它怎么想。"""
        row = await self._control_target(session_id)
        row = await self._run_control(
            row, self._orca.steer(session=row.orca_session, message=req.message)
        )
        await self._events.append(
            task_id=row.task_id,
            kind="session.steer",
            session_id=session_id,
            payload={"chars": len(req.message)},
        )
        await self._sessions.commit()
        return row

    async def kill(self, session_id: str, req: SessionKillRequest) -> SessionRow:
        """终止会话并释放租约（kill 即释放）。幂等：终态会话直接返回。"""
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        if row.state in ("killed", "failed", "recycled"):
            return row
        stop_lease_keeper(session_id)
        if row.orca_session:
            try:
                await self._orca.kill(session=row.orca_session, mode=req.mode)
            except OrcaSessionGone:
                pass  # 已经死了：kill 幂等，继续收尾释放租约
            except OrcaError as exc:
                raise OrcaCommandFailedError(str(exc)) from exc
        await self._tasks.release(row.task_id, worker=session_id)
        updated = await self._sessions.update(session_id, state="killed", ended_at=_now())
        await self._events.append(
            task_id=row.task_id,
            kind="session.killed",
            session_id=session_id,
            payload={"mode": req.mode},
        )
        await self._sessions.commit()
        return updated or row

    async def _mark_crashed(self, row: SessionRow, *, reason: str) -> None:
        """会话崩溃：任务转 failed（retryable，交给调度面重派）+ 租约释放 + 状态收尾。"""
        stop_lease_keeper(row.id)
        await self._tasks.fail(
            row.task_id,
            worker=row.id,
            error_type="session_crashed",
            retryable=True,
            context={"reason": reason[:500]},
        )
        await self._tasks.release(row.task_id, worker=row.id)
        await self._sessions.update(
            row.id, state="failed", error=reason[:500], ended_at=_now()
        )
        await self._events.append(
            task_id=row.task_id,
            kind="session.failed",
            session_id=row.id,
            payload={"reason": reason[:500], "retryable": True},
        )
        await self._sessions.commit()


class SessionQueryService:
    """会话视图数据 API（W06-I06，供 W10 嵌 Langfuse）。

    只读、轻量：<150ms 预算下只做一次索引查询 + 一次事件查询（前 20 条）；
    Langfuse 只给链接（前端 iframe 嵌入），本服务绝不代理其内容。
    """

    def __init__(self, *, sessions: SessionStore, events: EventStore) -> None:
        self._sessions = sessions
        self._events = events

    async def list_sessions(
        self,
        *,
        state: str | None = None,
        device_id: str | None = None,
        objective_id: str | None = None,
        runtime: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SessionRow], bool]:
        """按状态/设备/目标过滤；多取一条判断 has_more，省掉 count 查询。"""
        rows = await self._sessions.list_sessions(
            state=state,
            device_id=device_id,
            objective_id=objective_id,
            runtime=runtime,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        return rows[:limit], has_more

    async def session_detail(
        self, session_id: str, *, langfuse_base_url: str | None
    ) -> tuple[SessionRow, list, str | None]:
        row = await self._sessions.get(session_id)
        if row is None:
            raise SessionNotFoundError(f"会话不存在：{session_id}")
        events = await self._events.list_for_session(session_id, limit=20)
        langfuse_url = None
        if langfuse_base_url:
            langfuse_url = f"{langfuse_base_url.rstrip('/')}/trace/{row.task_id}"
        return row, events, langfuse_url
