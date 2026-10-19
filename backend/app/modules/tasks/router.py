"""任务队列与租约 —— HTTP 层（owner: W03）。

只做三件事：解析入参、调用 service、拼响应。**不写业务逻辑**。
所有写端点都必须带认证（W01 的 `OwnerDep`）；设备端 HMAC 身份在 W04 接入后
由 `frozen_readonly` 的 devices 模块提供，本窗口只预留 `device_id` 入参。

端点顺序有讲究：`/dead`、`/budget-status`、`/claim` 必须排在 `/{task_id}` 之前，
否则会被路径参数吃掉（FastAPI 按声明顺序匹配）。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import OwnerDep, Principal, require_scope
from app.core.errors import NotImplementedYetError

from . import service
from .machine import TaskState
from .schemas import (
    BudgetStatusOut,
    CancelIn,
    CandidatesOut,
    ClaimIn,
    CompleteIn,
    DeadLetterListOut,
    FailIn,
    HeartbeatIn,
    HeartbeatOut,
    LeaseIn,
    LeaseOut,
    RequeueIn,
    TaskCreate,
    TaskListOut,
    TaskOut,
)
from .settings import task_settings

MODULE = "tasks"
OWNER_WINDOW = "W03"

router = APIRouter(prefix="/tasks", tags=["tasks"])
DbDep = Annotated[AsyncSession, Depends(get_db)]
WriteDep = Annotated[Principal, Depends(require_scope("tasks:write"))]


@router.get("/_status", summary="模块状态（骨架自检用，保留以兼容冒烟测试）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "implemented"}


@router.get("/_ping", summary="认证链路探针（保留占位语义：501）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    # 冒烟测试用它验证「认证通过后的下游行为」；保留 501 语义，避免被业务误用。
    raise NotImplementedYetError("tasks 模块已有真实端点，请使用 /tasks 与 /tasks/claim")


# --------------------------------------------------------------------------- #
# 创建 / 列表 / 静态查询
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建任务（超预算直接进 held）",
)
async def create_task(payload: TaskCreate, session: DbDep, _: WriteDep) -> TaskOut:
    task = await service.create_task(session, payload)
    return service.to_out(task)


@router.get("", response_model=TaskListOut, summary="任务列表（按优先级与排队时间排序）")
async def list_tasks(
    session: DbDep,
    _: OwnerDep,
    state: Annotated[list[TaskState] | None, Query(description="可重复，按状态过滤")] = None,
    device_id: Annotated[str | None, Query(max_length=128)] = None,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    priority_min: Annotated[int | None, Query(ge=0, le=9)] = None,
    priority_max: Annotated[int | None, Query(ge=0, le=9)] = None,
    overdue_only: Annotated[bool, Query(description="只看排队超时任务")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskListOut:
    states = [s.value for s in state] if state else None
    rows = await service.list_tasks(
        session,
        states=states,
        device_id=device_id,
        goal_id=goal_id,
        priority_min=priority_min,
        priority_max=priority_max,
        overdue_only=overdue_only,
        limit=limit,
        offset=offset,
    )
    total = await service.count_tasks(
        session,
        states=states,
        device_id=device_id,
        priority_min=priority_min,
        priority_max=priority_max,
        overdue_only=overdue_only,
    )
    return TaskListOut(
        items=[service.to_out(row) for row in rows],
        count=total,
        limit=limit,
        offset=offset,
    )


@router.get("/dead", response_model=DeadLetterListOut, summary="死信列表（含原因）")
async def list_dead_letters(
    session: DbDep,
    _: OwnerDep,
    include_resolved: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeadLetterListOut:
    items, count = await service.list_dead_letters(
        session, include_resolved=include_resolved, limit=limit, offset=offset
    )
    return DeadLetterListOut(items=items, count=count)


@router.get("/budget-status", response_model=BudgetStatusOut, summary="预算总览（只读预聚合值）")
async def budget_status(session: DbDep, _: OwnerDep) -> BudgetStatusOut:
    return await service.budget_status(session)


# --------------------------------------------------------------------------- #
# 领取
# --------------------------------------------------------------------------- #
@router.post(
    "/claim",
    response_model=LeaseOut | None,
    responses={204: {"description": "当前没有可执行任务"}},
    summary="领取下一个可执行任务（FOR UPDATE SKIP LOCKED）",
)
async def claim(body: ClaimIn, session: DbDep, _: WriteDep) -> Any:
    result = await service.claim(session, body)
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    task, lease_seconds = result
    return LeaseOut(
        task=service.to_out(task),
        lease_token=task.lease_token,
        leased_until=task.leased_until,
        heartbeat_seconds=task_settings.heartbeat_seconds,
        lease_seconds=lease_seconds,
    )


# --------------------------------------------------------------------------- #
# 单任务读取与生命周期
# --------------------------------------------------------------------------- #
@router.get("/{task_id}", response_model=TaskOut, summary="任务详情")
async def get_task(task_id: uuid.UUID, session: DbDep, _: OwnerDep) -> TaskOut:
    return service.to_out(await service.get_task(session, task_id))


@router.get("/{task_id}/candidates", response_model=CandidatesOut, summary="候选设备与不可派原因")
async def candidates(task_id: uuid.UUID, session: DbDep, _: OwnerDep) -> CandidatesOut:
    return await service.candidates(session, task_id)


@router.post("/{task_id}/lease", response_model=LeaseOut, summary="指定任务领租（能力/预算校验）")
async def lease(task_id: uuid.UUID, body: LeaseIn, session: DbDep, _: WriteDep) -> LeaseOut:
    task, lease_seconds = await service.lease(session, task_id, body)
    return LeaseOut(
        task=service.to_out(task),
        lease_token=task.lease_token,
        leased_until=task.leased_until,
        heartbeat_seconds=task_settings.heartbeat_seconds,
        lease_seconds=lease_seconds,
    )


@router.post("/{task_id}/heartbeat", response_model=HeartbeatOut, summary="续租（30s 一次）")
async def heartbeat(
    task_id: uuid.UUID, body: HeartbeatIn, session: DbDep, _: WriteDep
) -> HeartbeatOut:
    task = await service.heartbeat(session, task_id, lease_token=body.lease_token)
    return HeartbeatOut(
        task_id=task.id,
        leased_until=task.leased_until,
        heartbeat_seconds=task_settings.heartbeat_seconds,
    )


@router.post("/{task_id}/start", response_model=TaskOut, summary="开始执行（leased → running）")
async def start(
    task_id: uuid.UUID, body: HeartbeatIn, session: DbDep, _: WriteDep
) -> TaskOut:
    return service.to_out(await service.start(session, task_id, lease_token=body.lease_token))


@router.post("/{task_id}/complete", response_model=TaskOut, summary="交货（→ done）")
async def complete(
    task_id: uuid.UUID, body: CompleteIn, session: DbDep, _: WriteDep
) -> TaskOut:
    task = await service.complete(
        session, task_id, lease_token=body.lease_token, result=body.result
    )
    return service.to_out(task)


@router.post("/{task_id}/fail", response_model=TaskOut, summary="失败上报（可重投则退避，否则进死信）")
async def fail(task_id: uuid.UUID, body: FailIn, session: DbDep, _: WriteDep) -> TaskOut:
    task = await service.fail(
        session,
        task_id,
        lease_token=body.lease_token,
        error=body.error,
        retryable=body.retryable,
    )
    return service.to_out(task)


@router.post("/{task_id}/cancel", response_model=TaskOut, summary="取消任务")
async def cancel(task_id: uuid.UUID, body: CancelIn, session: DbDep, _: WriteDep) -> TaskOut:
    return service.to_out(await service.cancel(session, task_id, reason=body.reason))


@router.post(
    "/{task_id}/requeue",
    response_model=TaskOut,
    summary="人工复活死信/失败任务（唯一复活入口）",
)
async def requeue(task_id: uuid.UUID, body: RequeueIn, session: DbDep, _: WriteDep) -> TaskOut:
    return service.to_out(await service.requeue(session, task_id, body))
