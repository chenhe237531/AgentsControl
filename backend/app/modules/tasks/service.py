"""任务服务层：业务规则与编排。

分层纪律：
- 不 import Request/Response/HTTPException —— 本层可在脚本、后台任务、未来 gRPC 里复用；
- 多步写入在一个事务里，失败由 `get_db` 统一回滚；
- 状态一律走 `machine.next_state()`，写入一律走 `repository`（本文件不出现 UPDATE）。

错误码（前端按 code 做文案，不解析 message）：
| code | HTTP | 场景 |
|---|---|---|
| `illegal_transition` | 409 | 当前状态不接受该事件 |
| `lease_conflict` | 409 | 两个 workers 同时抢同一任务，你慢了一步 |
| `lease_lost` | 409 | 令牌过期/任务已被回收，停止手上的活 |
| `capability_mismatch` | 409 | 设备能力不满足任务要求，拒绝派活 |
| `budget_exceeded` | 409 | 预算不足，任务已转入 held |
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError

from . import machine, matcher, repository, scheduler, valve
from .machine import TaskEvent, TaskState
from .models import Task
from .schemas import (
    BudgetStatusOut,
    CandidateOut,
    CandidatesOut,
    ClaimIn,
    DeadLetterOut,
    LeaseIn,
    RequeueIn,
    TaskCreate,
    TaskOut,
)
from .settings import TaskSettings, task_settings


def _now() -> datetime:
    return datetime.now(UTC)


def to_out(task: Task, settings: TaskSettings | None = None) -> TaskOut:
    return TaskOut.from_task(task, now=_now(), settings=settings)


async def _get(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = await repository.get_task(session, task_id)
    if task is None:
        raise NotFoundError("任务不存在")
    return task


# --------------------------------------------------------------------------- #
# 创建 / 查询
# --------------------------------------------------------------------------- #
async def create_task(
    session: AsyncSession, payload: TaskCreate, *, settings: TaskSettings | None = None
) -> Task:
    cfg = settings or task_settings
    decision = valve.evaluate(
        cost_spent=0,
        budget_cap=payload.budget_cap,
        reserve_ratio=cfg.budget_reserve_ratio,
    )
    state = TaskState.QUEUED.value if decision.allowed else TaskState.HELD.value
    task = await repository.create_task(
        session,
        title=payload.title,
        goal_id=payload.goal_id,
        payload=payload.payload,
        requires=payload.requires.model_dump(),
        priority=scheduler.clamp_priority(
            payload.priority, minimum=cfg.priority_min, maximum=cfg.priority_max
        ),
        state=state,
        budget_cap=payload.budget_cap,
        max_retries=payload.max_retries,
        retryable=payload.retryable,
        hold_reason=decision.hold_reason,
    )
    await session.commit()
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    states: Sequence[str] | None = None,
    device_id: str | None = None,
    goal_id: uuid.UUID | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    overdue_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    settings: TaskSettings | None = None,
) -> list[Task]:
    cfg = settings or task_settings
    return await repository.list_tasks(
        session,
        states=states,
        device_id=device_id,
        goal_id=goal_id,
        priority_min=priority_min,
        priority_max=priority_max,
        overdue_only=overdue_only,
        overdue_seconds=cfg.queued_overdue_seconds,
        limit=limit,
        offset=offset,
    )


async def count_tasks(
    session: AsyncSession,
    *,
    states: Sequence[str] | None = None,
    device_id: str | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    overdue_only: bool = False,
    settings: TaskSettings | None = None,
) -> int:
    cfg = settings or task_settings
    return await repository.count_tasks(
        session,
        states=states,
        device_id=device_id,
        priority_min=priority_min,
        priority_max=priority_max,
        overdue_only=overdue_only,
        overdue_seconds=cfg.queued_overdue_seconds,
    )


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    return await _get(session, task_id)


# --------------------------------------------------------------------------- #
# 领取与租约
# --------------------------------------------------------------------------- #
async def claim(
    session: AsyncSession, body: ClaimIn, *, settings: TaskSettings | None = None
) -> tuple[Task, int] | None:
    """领取下一个可执行任务。返回 `(task, lease_seconds)`；无活可干返回 None。

    公平性：同一设备连续领走高优任务达到阈值后，本回合强制只看低优任务；
    若队列里根本没有低优任务，则退化为正常优先级（不能让策略把队列卡死）。
    """
    cfg = settings or task_settings
    lease_seconds = cfg.clamp_lease(body.lease_seconds)

    stats = await repository.get_device_stats(session, body.device_id)
    consecutive_high = stats.consecutive_high if stats else 0
    force_low = scheduler.should_force_low(consecutive_high, cfg.fairness_burst)

    task = await repository.claim_next(
        session,
        device_id=body.device_id,
        caps=body.caps,
        lease_seconds=lease_seconds,
        settings=cfg,
        force_low=force_low,
    )
    if task is None and force_low:
        task = await repository.claim_next(
            session,
            device_id=body.device_id,
            caps=body.caps,
            lease_seconds=lease_seconds,
            settings=cfg,
            force_low=False,
        )
    if task is None:
        await session.commit()
        return None

    await repository.touch_device_stats(
        session,
        body.device_id,
        consecutive_high=scheduler.next_consecutive_high(
            consecutive_high,
            task.priority,
            high_max=cfg.high_priority_max,
            low_min=cfg.low_priority_min,
        ),
    )
    await session.commit()
    return task, lease_seconds


async def lease(
    session: AsyncSession, task_id: uuid.UUID, body: LeaseIn, *, settings: TaskSettings | None = None
) -> tuple[Task, int]:
    cfg = settings or task_settings
    task = await _get(session, task_id)

    # 先让状态机裁决（非法状态直接 409，message 里带 allowed_events）
    machine.next_state(task.state, TaskEvent.LEASE)

    decision = valve.evaluate(
        cost_spent=task.cost_spent,
        budget_cap=task.budget_cap,
        reserve_ratio=cfg.budget_reserve_ratio,
    )
    if not decision.allowed:
        # 派活前拦截：不派出去，转入 held 并留下原因，等人处理
        await repository.hold(session, task.id, reason=decision.reason or "预算不足")
        await session.commit()
        raise ConflictError(
            decision.reason or "预算不足",
            code="budget_exceeded",
            details={"budget_cap": str(task.budget_cap), "cost_spent": str(task.cost_spent)},
        )

    match = matcher.evaluate(task.requires, body.caps)
    if not match.eligible:
        raise ConflictError(
            "设备能力不满足任务要求",
            code="capability_mismatch",
            details={"reasons": list(match.reasons), "device_id": body.device_id},
        )

    lease_seconds = cfg.clamp_lease(body.lease_seconds)
    leased = await repository.lease_task(
        session, task.id, device_id=body.device_id, lease_seconds=lease_seconds
    )
    if leased is None:
        raise ConflictError(
            "任务已被其它工作端领取",
            code="lease_conflict",
            details={"task_id": str(task_id)},
        )
    await session.commit()
    return leased, lease_seconds


async def heartbeat(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    settings: TaskSettings | None = None,
) -> Task:
    cfg = settings or task_settings
    task = await _get(session, task_id)
    if task.state not in {s.value for s in machine.LEASED_STATES}:
        raise ConflictError(
            "任务当前不持有租约",
            code="lease_lost",
            details={"state": task.state},
        )
    updated = await repository.heartbeat(
        session, task.id, lease_token=lease_token, lease_seconds=cfg.lease_seconds
    )
    if updated is None:
        raise ConflictError("租约已失效（可能已被回收或转手）", code="lease_lost")
    await session.commit()
    return updated


async def start(
    session: AsyncSession, task_id: uuid.UUID, *, lease_token: uuid.UUID
) -> Task:
    task = await _get(session, task_id)
    machine.next_state(task.state, TaskEvent.START)
    updated = await repository.start(session, task.id, lease_token=lease_token)
    if updated is None:
        raise ConflictError("租约已失效或任务不在 leased 状态", code="lease_lost")
    await session.commit()
    return updated


async def complete(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    result: dict[str, Any] | None,
) -> Task:
    task = await _get(session, task_id)
    machine.next_state(task.state, TaskEvent.COMPLETE)
    updated = await repository.complete(
        session, task.id, lease_token=lease_token, result=result
    )
    if updated is None:
        raise ConflictError("租约已失效，交货被拒绝", code="lease_lost")
    await session.commit()
    return updated


# --------------------------------------------------------------------------- #
# 失败与重试
# --------------------------------------------------------------------------- #
async def fail(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    error: str,
    retryable: bool | None = None,
    settings: TaskSettings | None = None,
) -> Task:
    """失败上报 → 可重投则退避重投，否则进死信（绝不静默丢弃）。"""
    cfg = settings or task_settings
    task = await _get(session, task_id)
    machine.next_state(task.state, TaskEvent.FAIL)

    attempts = task.retry_count + 1
    can_retry = task.retryable if retryable is None else retryable
    exhausted = scheduler.is_retry_exhausted(attempts, task.max_retries)
    will_retry = can_retry and not exhausted
    next_retry_at = (
        _now() + timedelta(seconds=scheduler.backoff_seconds(
            attempts, base_seconds=cfg.retry_base_seconds, max_seconds=cfg.retry_max_seconds
        ))
        if will_retry
        else None
    )

    updated = await repository.fail(
        session,
        task.id,
        lease_token=lease_token,
        retry_count=attempts,
        next_retry_at=next_retry_at,
        last_error=error[:2000],
    )
    if updated is None:
        raise ConflictError("租约已失效，失败上报被拒绝", code="lease_lost")

    if not will_retry:
        reason = "non_retryable" if not can_retry else "max_retries_exhausted"
        detail = (
            "工作端声明该失败不可重试"
            if not can_retry
            else f"重试次数已耗尽（{attempts}/{task.max_retries}）"
        )
        dead = await repository.mark_dead(
            session, task.id, expected_state=TaskState.FAILED, detail=detail
        )
        if dead is not None:
            await repository.add_dead_letter(
                session, task=dead, reason=reason, detail=f"{detail}；最后错误：{error[:500]}"
            )
            updated = dead

    await session.commit()
    return updated


async def reclaim_expired(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    settings: TaskSettings | None = None,
) -> dict[str, Any]:
    """回收过期租约。返回 `{reclaimed, dead, dry_run, ids}`，供脚本与运维核对。"""
    cfg = settings or task_settings
    batch = limit or cfg.reclaim_batch
    rows = await repository.reclaim_expired(
        session, limit=batch, reclaim_max=cfg.reclaim_max, dry_run=dry_run
    )

    dead_ids: list[str] = []
    if not dry_run:
        for task_id, state, reclaim_count, last_error in rows:
            if state != TaskState.DEAD.value:
                continue
            task = await repository.get_task(session, task_id)
            if task is None:  # pragma: no cover - 并发删除
                continue
            await repository.add_dead_letter(
                session,
                task=task,
                reason="lease_reclaimed",
                detail=(
                    f"租约连续 {reclaim_count} 次过期被回收（阈值 {cfg.reclaim_max}）；"
                    f"最后错误：{last_error or 'lease_expired'}"
                ),
            )
            dead_ids.append(str(task_id))
        await session.commit()

    return {
        "reclaimed": len(rows),
        "dead": len(dead_ids),
        "dead_ids": dead_ids,
        "ids": [str(row[0]) for row in rows],
        "dry_run": dry_run,
    }


async def promote_due_retries(
    session: AsyncSession, *, limit: int = 200
) -> list[uuid.UUID]:
    ids = await repository.promote_due_retries(session, limit=limit)
    await session.commit()
    return ids


# --------------------------------------------------------------------------- #
# 人工操作
# --------------------------------------------------------------------------- #
async def requeue(
    session: AsyncSession, task_id: uuid.UUID, body: RequeueIn
) -> Task:
    task = await _get(session, task_id)
    # 只允许失败/死信复活；其它状态由状态机给出 409
    machine.next_state(task.state, TaskEvent.REQUEUE)
    updated = await repository.requeue(
        session,
        task.id,
        expected_states=(TaskState.DEAD, TaskState.FAILED),
        reset_counters=body.reset_counters,
    )
    if updated is None:
        raise ConflictError("任务状态已变化，请刷新后重试", code="illegal_transition")
    await repository.resolve_dead_letters(session, task.id, resolution="requeue")
    await session.commit()
    return updated


async def cancel(session: AsyncSession, task_id: uuid.UUID, *, reason: str) -> Task:
    task = await _get(session, task_id)
    cancellable = tuple(
        state for state in machine.all_states() if machine.can(state, TaskEvent.CANCEL)
    )
    if TaskState(task.state) not in cancellable:
        raise ConflictError(
            f"任务状态 {task.state} 不可取消",
            code="illegal_transition",
            details={"state": task.state},
        )
    updated = await repository.cancel(
        session, task.id, expected_states=cancellable, reason=reason
    )
    if updated is None:
        raise ConflictError("任务状态已变化，请刷新后重试", code="illegal_transition")
    await session.commit()
    return updated


# --------------------------------------------------------------------------- #
# 候选设备（只读 W04 的 devices）
# --------------------------------------------------------------------------- #
async def candidates(
    session: AsyncSession, task_id: uuid.UUID
) -> CandidatesOut:
    task = await _get(session, task_id)

    exists = await session.scalar(sa.text("SELECT to_regclass('public.devices') IS NOT NULL"))
    if not exists:
        return CandidatesOut(
            task_id=task.id,
            candidates=[],
            note="设备目录尚未落地（W04-I01），暂时无法给出候选设备",
        )

    columns = {
        str(row[0])
        for row in (
            await session.execute(
                sa.text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'devices'
                    """
                )
            )
        ).all()
    }
    if not {"id", "caps"}.issubset(columns):
        return CandidatesOut(
            task_id=task.id,
            candidates=[],
            note="devices 表结构未就绪（缺少 id/caps 列）",
        )

    select_name = "name" if "name" in columns else "NULL::text AS name"
    rows = (
        await session.execute(
            sa.text(f"SELECT id::text AS id, {select_name}, caps FROM devices LIMIT 200")
        )
    ).mappings().all()

    ranked = matcher.rank(
        [(str(row["id"]), row["name"], row["caps"] or {}) for row in rows],
        task.requires,
    )
    candidates = [
        CandidateOut(device_id=device_id, name=name, eligible=result.eligible, reasons=list(result.reasons))
        for device_id, name, result in ranked
    ]
    eligible_count = sum(1 for c in candidates if c.eligible)
    note = (
        f"{eligible_count}/{len(candidates)} 台设备可派"
        if candidates
        else "设备目录为空：还没有设备上报过能力"
    )
    return CandidatesOut(task_id=task.id, candidates=candidates, note=note)


# --------------------------------------------------------------------------- #
# 死信 / 预算总览 / 记账入口
# --------------------------------------------------------------------------- #
async def list_dead_letters(
    session: AsyncSession,
    *,
    include_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[DeadLetterOut], int]:
    rows = await repository.list_dead_letters(
        session, include_resolved=include_resolved, limit=limit, offset=offset
    )
    items = [
        DeadLetterOut(
            id=letter.id,
            task_id=letter.task_id,
            reason=letter.reason,
            detail=letter.detail,
            reclaim_count=letter.reclaim_count,
            retry_count=letter.retry_count,
            created_at=letter.created_at,
            resolved_at=letter.resolved_at,
            resolution=letter.resolution,
            task_title=title,
            task_priority=priority,
        )
        for letter, title, priority in rows
    ]
    return items, len(items)


async def budget_status(
    session: AsyncSession, *, settings: TaskSettings | None = None
) -> BudgetStatusOut:
    cfg = settings or task_settings
    totals = await repository.budget_totals(session)
    cap_total = Decimal(str(totals["cap_total"]))
    spent_total = Decimal(str(totals["spent_total"]))
    return BudgetStatusOut(
        tasks_with_cap=int(totals["tasks_with_cap"]),
        tasks_held=int(totals["tasks_held"]),
        cap_total=cap_total,
        spent_total=spent_total,
        reserve_ratio=cfg.budget_reserve_ratio,
        available_total=valve.available_total(
            cap_total=cap_total,
            spent_total=spent_total,
            reserve_ratio=cfg.budget_reserve_ratio,
        ),
    )


async def record_cost(session: AsyncSession, task_id: uuid.UUID, amount: Decimal) -> None:
    """给 W06/W07 记账逻辑用的公开入口（不挂 HTTP 路由）。"""
    await repository.record_cost(session, task_id, amount)
