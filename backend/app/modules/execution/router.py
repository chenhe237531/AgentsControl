"""执行面与 Worker/Orca 模块 —— owner: W06

HTTP 端点分四组（按 issue 顺序落地）：
- Worker 协议：claim / heartbeat / complete / fail（W06-I01）
- Orca 自检：GET /orca/health（W06-I02）——探测失败 → available=false，拒绝派 Orca 类任务
- 会话生命周期：POST /sessions、DELETE /sessions/{id}（W06-I03）
- 会话控制与归集：spawn/send/steer/kill/harvest（W06-I04、W06-I05）
- 会话视图：GET /sessions、GET /sessions/{id}（W06-I06）

分层约定（不可越界）：
- `router.py`      只做 HTTP：解析入参、调用 service、格式化响应，**不写业务逻辑**
- `service.py`     业务规则与编排，**不依赖 Request/Response**，多步写入走事务
- `repository.py`  数据访问，只接受/返回领域对象或行记录
- `models.py`      ORM 模型，继承 `app.core.db.Base`

认证说明：并行开发期沿用 OwnerDep（单用户 owner 令牌）；Worker 独立身份
（device token + scope）在 W01-I02 RBAC 落地后收紧。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import OwnerDep
from app.core.errors import NotImplementedYetError
from app.modules.execution import orca as exec_orca
from app.modules.execution import protocol as proto
from app.modules.execution import service as svc
from app.modules.execution import worktree as exec_worktree
from app.modules.execution.repository import (
    EventStore,
    SessionRow,
    SessionStore,
    SqlEventStore,
    SqlSessionStore,
    SqlWorkerStore,
    WorkerStore,
)
from app.modules.execution.schemas import (
    OrcaHealthView,
    SessionAck,
    SessionCreateRequest,
    SessionKillRequest,
    SessionSendRequest,
    SessionSpawnRequest,
    SessionSteerRequest,
    SessionView,
)

MODULE = "execution"
OWNER_WINDOW = "W06"

router = APIRouter(prefix="/execution", tags=["execution"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------- 依赖装配

def get_worker_store(db: DbDep) -> WorkerStore:
    return SqlWorkerStore(db)


def get_event_store(db: DbDep) -> EventStore:
    return SqlEventStore(db)


def get_tasks_gateway() -> proto.TasksGateway:
    return proto.TasksServiceGateway()


def get_worker_service(
    workers: Annotated[WorkerStore, Depends(get_worker_store)],
    events: Annotated[EventStore, Depends(get_event_store)],
    tasks: Annotated[proto.TasksGateway, Depends(get_tasks_gateway)],
) -> svc.WorkerService:
    return svc.WorkerService(workers=workers, events=events, tasks=tasks)


WorkerServiceDep = Annotated[svc.WorkerService, Depends(get_worker_service)]


def get_session_store(db: DbDep) -> SessionStore:
    return SqlSessionStore(db)


def get_worktree_manager() -> exec_worktree.WorktreeManager:
    return exec_worktree.default_manager()


def get_orca_adapter() -> exec_orca.OrcaAdapter:
    return exec_orca.OrcaAdapter(exec_orca.CliRunner())


def get_session_service(
    sessions: Annotated[SessionStore, Depends(get_session_store)],
    events: Annotated[EventStore, Depends(get_event_store)],
    worktrees: Annotated[exec_worktree.WorktreeManager, Depends(get_worktree_manager)],
    tasks: Annotated[proto.TasksGateway, Depends(get_tasks_gateway)],
    orca: Annotated[exec_orca.OrcaAdapter, Depends(get_orca_adapter)],
) -> svc.SessionService:
    return svc.SessionService(
        sessions=sessions, events=events, worktrees=worktrees, tasks=tasks, orca=orca
    )


SessionServiceDep = Annotated[svc.SessionService, Depends(get_session_service)]


def _session_view(row: SessionRow) -> SessionView:
    return SessionView(**asdict(row))


# ---------------------------------------------------------------- 骨架自检

@router.get("/_status", summary="模块状态（并行开发期自检）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "active"}


@router.get("/_ping")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    """并行开发期的窗口占位端点（返回 501）。

    按惯例实现后应删除，但 openapi.snapshot.json 基线仍收录本路径：
    删除/改动会让 plan job 的 OpenAPI 契约检查报「removed/changed」而失败。
    等 marshal 在共享 PR 里重建快照基线时一并移除（对 W09 的 handoff 已注明）。
    """
    raise NotImplementedYetError(MODULE)


# ---------------------------------------------------------------- Orca 自检（W06-I02）

@router.get(
    "/orca/health",
    summary="Orca 能力自检（CLI/版本/agent 种类/worktree 根可写），结果缓存 10 分钟",
    response_model=OrcaHealthView,
)
async def orca_health(_: OwnerDep, force: bool = False) -> OrcaHealthView:
    """探测失败 → available=false；派 Orca 类任务前必须先看这里。"""
    result = await exec_orca.get_orca_status(force=force)
    return OrcaHealthView(
        available=result.available,
        checked_at=result.checked_at,
        cli_path=result.cli_path,
        version=result.version,
        agents=list(result.agents),
        worktree_root=result.worktree_root,
        worktree_root_writable=result.worktree_root_writable,
        error=result.error,
        details=result.details,
    )


# ---------------------------------------------------------------- 会话生命周期（W06-I03）

@router.post(
    "/sessions",
    status_code=201,
    summary="创建会话：建 worktree（.worktrees/<issue>-<slug>）并写 .fleet-task.json",
    response_model=SessionView,
)
async def create_session(
    body: SessionCreateRequest,
    _: OwnerDep,
    service: SessionServiceDep,
) -> SessionView:
    row = await service.create_session(body)
    return _session_view(row)


@router.delete(
    "/sessions/{session_id}",
    summary="回收会话：未提交改动会拒绝并返回文件清单（?force=1 强制丢弃）",
    response_model=SessionView,
)
async def recycle_session(
    session_id: str,
    _: OwnerDep,
    service: SessionServiceDep,
    force: bool = False,
) -> SessionView:
    row = await service.recycle_session(session_id, force=force)
    return _session_view(row)


# ---------------------------------------------------------------- 会话控制（W06-I04）

@router.post(
    "/sessions/{session_id}/spawn",
    summary="启动会话：probe 可用才派；成功转 running 并自动续租（会话即租约）",
    response_model=SessionView,
)
async def spawn_session(
    session_id: str,
    body: SessionSpawnRequest,
    _: OwnerDep,
    service: SessionServiceDep,
) -> SessionView:
    row = await service.spawn(session_id, body)
    return _session_view(row)


@router.post(
    "/sessions/{session_id}/send",
    summary="向 running 会话注入 prompt（附上下文文件）；会话已死则任务转 failed 并释放租约",
    response_model=SessionAck,
)
async def send_to_session(
    session_id: str,
    body: SessionSendRequest,
    _: OwnerDep,
    service: SessionServiceDep,
) -> SessionAck:
    row = await service.send(session_id, body)
    return SessionAck(session_id=row.id, task_id=row.task_id, state=row.state)


@router.post(
    "/sessions/{session_id}/steer",
    summary="人工干预（闸门/纠偏）：只做生命周期级指令，过程细节靠事件表",
    response_model=SessionAck,
)
async def steer_session(
    session_id: str,
    body: SessionSteerRequest,
    _: OwnerDep,
    service: SessionServiceDep,
) -> SessionAck:
    row = await service.steer(session_id, body)
    return SessionAck(session_id=row.id, task_id=row.task_id, state=row.state)


@router.post(
    "/sessions/{session_id}/kill",
    summary="终止会话并释放租约（kill 即释放）；graceful 先 snapshot 再杀",
    response_model=SessionView,
)
async def kill_session(
    session_id: str,
    body: SessionKillRequest,
    _: OwnerDep,
    service: SessionServiceDep,
) -> SessionView:
    row = await service.kill(session_id, body)
    return _session_view(row)


# ---------------------------------------------------------------- Worker 协议（W06-I01）

@router.post(
    "/workers/claim",
    summary="Worker 领活：能力标签 + 并发余量 → 任务 + 租约；无匹配任务返回 204",
    responses={200: {"model": proto.WorkerClaimResponse}, 204: {"description": "队列中无匹配任务"}},
)
async def workers_claim(
    body: proto.WorkerClaimRequest,
    _: OwnerDep,
    service: WorkerServiceDep,
) -> Response:
    claim = await service.claim(body)
    if claim is None:
        return Response(status_code=204)
    return Response(
        content=claim.model_dump_json(),
        media_type="application/json",
        status_code=200,
    )


@router.post(
    "/workers/{worker_id}/heartbeat",
    summary="Worker 续约：携带进度则落事件，纯续约只刷新租约",
    response_model=proto.WorkerHeartbeatResponse,
)
async def workers_heartbeat(
    worker_id: str,
    body: proto.WorkerHeartbeatRequest,
    _: OwnerDep,
    service: WorkerServiceDep,
) -> proto.WorkerHeartbeatResponse:
    return await service.heartbeat(worker_id, body)


@router.post(
    "/workers/{worker_id}/complete",
    summary="Worker 交货：result 必带产物引用与成本，写 task_events 供 W07 归集",
    response_model=proto.WorkerCompleteResponse,
)
async def workers_complete(
    worker_id: str,
    body: proto.WorkerCompleteRequest,
    _: OwnerDep,
    service: WorkerServiceDep,
) -> proto.WorkerCompleteResponse:
    return await service.complete(worker_id, body)


@router.post(
    "/workers/{worker_id}/fail",
    summary="Worker 报错：retryable 决定回队或死信（由 tasks 状态机裁决）",
    response_model=proto.WorkerFailResponse,
)
async def workers_fail(
    worker_id: str,
    body: proto.WorkerFailRequest,
    _: OwnerDep,
    service: WorkerServiceDep,
) -> proto.WorkerFailResponse:
    return await service.fail(worker_id, body)
