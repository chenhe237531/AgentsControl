"""业务编排：Worker 协议的四步生命周期（claim → heartbeat → complete/fail）。

分层纪律：
- 不感知 Request/Response；
- 任务状态只通过 TasksGateway（tasks.service 公开函数）流转，本模块不写 tasks 表；
- 每次「对外可见的语义变化」都落一条 task_events（claimed/completed/failed），
  heartbeat 仅在携带 progress/step 时落事件（避免事件表被纯续约灌爆）。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

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
from .repository import EventStore, WorkerInfo, WorkerStore


def _now() -> datetime:
    return datetime.now(UTC)


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
