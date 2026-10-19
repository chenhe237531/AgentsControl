"""数据访问层：任务队列的全部写路径都在这里。

为什么把 SQL 全挤在一个文件：
- 队列的正确性靠**单条原子语句**（`FOR UPDATE SKIP LOCKED`、`UPDATE ... WHERE leased_until < now()`），
  这些语句散落到 service 里就没人能一眼看出竞态点；
- 状态迁移只允许走 `machine`，本文件的每条写语句都把「期望状态」写进 WHERE，
  并在执行前用 `machine.next_state()` 断言目标状态（`_assert_target`），规则漂移会立刻炸。

时间一律用数据库 `now()`：应用与数据库时钟不同步时，租约提前/延后回收都是事故。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from . import machine, matcher, valve
from .machine import TaskEvent, TaskState
from .models import DeadLetter, Task, TaskDeviceStats
from .settings import TaskSettings, task_settings


def _assert_target(state: TaskState | str, event: TaskEvent | str, target: TaskState | str) -> None:
    """写死的目标状态必须与状态机一致——不一致就说明有人绕过了规则。"""
    expected = machine.next_state(state, event)
    if expected != TaskState(target):
        raise AssertionError(
            f"状态机漂移：{state} --{event}--> 期望 {expected.value}，SQL 实际写 {target}"
        )


async def _reload(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id, populate_existing=True)


# --------------------------------------------------------------------------- #
# 创建与读取
# --------------------------------------------------------------------------- #
async def create_task(
    session: AsyncSession,
    *,
    title: str,
    goal_id: uuid.UUID | None,
    payload: dict[str, Any],
    requires: dict[str, Any],
    priority: int,
    state: str,
    budget_cap: Decimal | None,
    max_retries: int,
    retryable: bool,
    hold_reason: str | None = None,
) -> Task:
    task = Task(
        title=title,
        goal_id=goal_id,
        payload=payload,
        requires=requires,
        priority=priority,
        state=state,
        budget_cap=budget_cap,
        max_retries=max_retries,
        retryable=retryable,
        hold_reason=hold_reason,
    )
    session.add(task)
    await session.flush()
    await session.refresh(task)
    return task


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    return await session.get(Task, task_id)


async def list_tasks(
    session: AsyncSession,
    *,
    states: Sequence[str] | None = None,
    device_id: str | None = None,
    goal_id: uuid.UUID | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    overdue_only: bool = False,
    overdue_seconds: int = 0,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    stmt = sa.select(Task)
    if states:
        stmt = stmt.where(Task.state.in_(list(states)))
    if device_id:
        stmt = stmt.where(Task.device_id == device_id)
    if goal_id:
        stmt = stmt.where(Task.goal_id == goal_id)
    if priority_min is not None:
        stmt = stmt.where(Task.priority >= priority_min)
    if priority_max is not None:
        stmt = stmt.where(Task.priority <= priority_max)
    if overdue_only and overdue_seconds > 0:
        cutoff = datetime.now(UTC) - timedelta(seconds=overdue_seconds)
        stmt = stmt.where(Task.state == TaskState.QUEUED.value, Task.queued_at <= cutoff)
    stmt = stmt.order_by(Task.priority.asc(), Task.queued_at.asc()).limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def count_tasks(
    session: AsyncSession,
    *,
    states: Sequence[str] | None = None,
    device_id: str | None = None,
    priority_min: int | None = None,
    priority_max: int | None = None,
    overdue_only: bool = False,
    overdue_seconds: int = 0,
) -> int:
    """与 `list_tasks` 使用同一组过滤条件——否则分页总数会和列表对不上。"""
    stmt = sa.select(sa.func.count()).select_from(Task)
    if states:
        stmt = stmt.where(Task.state.in_(list(states)))
    if device_id:
        stmt = stmt.where(Task.device_id == device_id)
    if priority_min is not None:
        stmt = stmt.where(Task.priority >= priority_min)
    if priority_max is not None:
        stmt = stmt.where(Task.priority <= priority_max)
    if overdue_only and overdue_seconds > 0:
        cutoff = datetime.now(UTC) - timedelta(seconds=overdue_seconds)
        stmt = stmt.where(Task.state == TaskState.QUEUED.value, Task.queued_at <= cutoff)
    return int((await session.scalar(stmt)) or 0)


# --------------------------------------------------------------------------- #
# 队列：领取 / 租约 / 心跳
# --------------------------------------------------------------------------- #
async def claim_next(
    session: AsyncSession,
    *,
    device_id: str,
    caps: dict[str, bool],
    lease_seconds: int,
    settings: TaskSettings | None = None,
    force_low: bool = False,
) -> Task | None:
    """领取下一个可执行任务。**单条语句**完成「选一个 + 上锁 + 改状态」。

    `FOR UPDATE SKIP LOCKED` 是队列本身：两个 worker 同时进来只会各自拿到不同的行，
    谁也不会阻塞在对方的事务上。能力与预算过滤都在同一条语句里，
    避免「先领到再退回」造成的高优任务抖动。
    """
    cfg = settings or task_settings
    conditions = [
        "t.state = 'queued'",
        "(t.next_retry_at IS NULL OR t.next_retry_at <= now())",
        *matcher.requires_sql_conditions("t"),
        valve.LEASE_BUDGET_PREDICATE,
    ]
    if force_low:
        conditions.append("t.priority >= :low_priority_min")

    sql = f"""
        WITH candidate AS (
            SELECT t.id
            FROM tasks t
            WHERE {' AND '.join(conditions)}
            ORDER BY t.priority ASC, t.queued_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE tasks t
        SET state = 'leased',
            device_id = :device_id,
            lease_token = gen_random_uuid(),
            leased_until = now() + make_interval(secs => :lease_seconds),
            heartbeat_at = now(),
            next_retry_at = NULL,
            hold_reason = NULL,
            updated_at = now()
        FROM candidate c
        WHERE t.id = c.id
        RETURNING t.id
    """
    # 状态机断言：queued --lease--> leased
    _assert_target(TaskState.QUEUED, TaskEvent.LEASE, TaskState.LEASED)

    row = (
        await session.execute(
            sa.text(sql),
            {
                "device_id": device_id,
                "lease_seconds": float(lease_seconds),
                "has_gui": bool(caps.get("has_gui", False)),
                "has_orca": bool(caps.get("has_orca", False)),
                "has_browser": bool(caps.get("has_browser", False)),
                "can_sleep": bool(caps.get("can_sleep", False)),
                "budget_reserve_ratio": cfg.budget_reserve_ratio,
                "low_priority_min": cfg.low_priority_min,
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, row[0])


async def lease_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    device_id: str,
    lease_seconds: int,
) -> Task | None:
    """指定任务领租。返回 None 表示「被别人抢先」，由服务层转成 409。"""
    _assert_target(TaskState.QUEUED, TaskEvent.LEASE, TaskState.LEASED)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'leased',
            device_id = :device_id,
            lease_token = gen_random_uuid(),
            leased_until = now() + make_interval(secs => :lease_seconds),
            heartbeat_at = now(),
            next_retry_at = NULL,
            hold_reason = NULL,
            updated_at = now()
        WHERE id = :task_id
          AND state = 'queued'
          AND (next_retry_at IS NULL OR next_retry_at <= now())
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "device_id": device_id,
                "lease_seconds": float(lease_seconds),
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def heartbeat(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    lease_seconds: int,
) -> Task | None:
    """续租。令牌不匹配 = 租约已被回收/转手，返回 None（服务层转 409 lease_lost）。"""
    _assert_target(TaskState.LEASED, TaskEvent.HEARTBEAT, TaskState.LEASED)
    _assert_target(TaskState.RUNNING, TaskEvent.HEARTBEAT, TaskState.RUNNING)
    sql = sa.text(
        """
        UPDATE tasks
        SET leased_until = now() + make_interval(secs => :lease_seconds),
            heartbeat_at = now(),
            updated_at = now()
        WHERE id = :task_id
          AND lease_token = :lease_token
          AND state IN ('leased', 'running')
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "lease_token": lease_token,
                "lease_seconds": float(lease_seconds),
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def start(session: AsyncSession, task_id: uuid.UUID, *, lease_token: uuid.UUID) -> Task | None:
    """`leased → running`，语义上表示工作端真正开始执行（可选步骤）。"""
    _assert_target(TaskState.LEASED, TaskEvent.START, TaskState.RUNNING)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'running', started_at = now(), updated_at = now()
        WHERE id = :task_id AND lease_token = :lease_token AND state = 'leased'
        RETURNING id
        """
    )
    row = (
        await session.execute(sql, {"task_id": task_id, "lease_token": lease_token})
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def complete(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    result: dict[str, Any] | None,
) -> Task | None:
    _assert_target(TaskState.LEASED, TaskEvent.COMPLETE, TaskState.DONE)
    _assert_target(TaskState.RUNNING, TaskEvent.COMPLETE, TaskState.DONE)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'done',
            result = CAST(:result AS jsonb),
            finished_at = now(),
            started_at = COALESCE(started_at, now()),
            lease_token = NULL,
            leased_until = NULL,
            heartbeat_at = NULL,
            last_error = NULL,
            updated_at = now()
        WHERE id = :task_id
          AND lease_token = :lease_token
          AND state IN ('leased', 'running')
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "lease_token": lease_token,
                "result": None if result is None else json.dumps(result, ensure_ascii=False),
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def fail(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    lease_token: uuid.UUID,
    retry_count: int,
    next_retry_at: datetime | None,
    last_error: str,
) -> Task | None:
    """`leased/running → failed`。是否重投由服务层根据退避与次数决定。"""
    _assert_target(TaskState.LEASED, TaskEvent.FAIL, TaskState.FAILED)
    _assert_target(TaskState.RUNNING, TaskEvent.FAIL, TaskState.FAILED)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'failed',
            retry_count = :retry_count,
            next_retry_at = :next_retry_at,
            last_error = :last_error,
            finished_at = now(),
            lease_token = NULL,
            leased_until = NULL,
            heartbeat_at = NULL,
            updated_at = now()
        WHERE id = :task_id
          AND lease_token = :lease_token
          AND state IN ('leased', 'running')
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "lease_token": lease_token,
                "retry_count": retry_count,
                "next_retry_at": next_retry_at,
                "last_error": last_error,
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


# --------------------------------------------------------------------------- #
# 回收 / 重试 / 死信
# --------------------------------------------------------------------------- #
async def reclaim_expired(
    session: AsyncSession,
    *,
    limit: int,
    reclaim_max: int,
    dry_run: bool = False,
) -> list[tuple[uuid.UUID, str, int, str | None]]:
    """回收过期租约。返回 `(task_id, new_state, reclaim_count, last_error)`。

    一条 SQL 同时完成「选中过期租约 + 计数 + 决定 queued/dead」，
    不做「先 SELECT 再 UPDATE」——那中间有窗口，两个清理进程会重复计数。
    """
    _assert_target(TaskState.LEASED, TaskEvent.RECLAIM, TaskState.QUEUED)
    _assert_target(TaskState.RUNNING, TaskEvent.RECLAIM, TaskState.QUEUED)
    _assert_target(TaskState.LEASED, TaskEvent.RECLAIM_EXHAUSTED, TaskState.DEAD)
    _assert_target(TaskState.RUNNING, TaskEvent.RECLAIM_EXHAUSTED, TaskState.DEAD)

    if dry_run:
        rows = (
            await session.execute(
                sa.text(
                    """
                    SELECT id, leased_until, device_id, reclaim_count
                    FROM tasks
                    WHERE state IN ('leased', 'running')
                      AND leased_until IS NOT NULL
                      AND leased_until < now()
                    ORDER BY leased_until ASC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        ).all()
        return [(row[0], TaskState.QUEUED.value, int(row[3]), None) for row in rows]

    sql = sa.text(
        """
        WITH expired AS (
            SELECT id
            FROM tasks
            WHERE state IN ('leased', 'running')
              AND leased_until IS NOT NULL
              AND leased_until < now()
            ORDER BY leased_until ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE tasks t
        SET reclaim_count = t.reclaim_count + 1,
            state = CASE
                WHEN t.reclaim_count + 1 >= :reclaim_max THEN 'dead'
                ELSE 'queued'
            END,
            device_id = NULL,
            lease_token = NULL,
            leased_until = NULL,
            heartbeat_at = NULL,
            next_retry_at = NULL,
            queued_at = now(),
            hold_reason = NULL,
            last_error = COALESCE(t.last_error, 'lease_expired'),
            updated_at = now()
        FROM expired e
        WHERE t.id = e.id
        RETURNING t.id, t.state, t.reclaim_count, t.last_error
        """
    )
    rows = (
        await session.execute(sql, {"limit": limit, "reclaim_max": reclaim_max})
    ).all()
    return [(row[0], str(row[1]), int(row[2]), row[3]) for row in rows]


async def promote_due_retries(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """把到达退避时间的 `failed` 任务放回队列。批量单条 SQL，幂等。"""
    _assert_target(TaskState.FAILED, TaskEvent.RETRY, TaskState.QUEUED)
    sql = sa.text(
        """
        WITH due AS (
            SELECT id
            FROM tasks
            WHERE state = 'failed'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= now()
            ORDER BY next_retry_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE tasks t
        SET state = 'queued',
            next_retry_at = NULL,
            queued_at = now(),
            finished_at = NULL,
            updated_at = now()
        FROM due d
        WHERE t.id = d.id
        RETURNING t.id
        """
    )
    rows = (await session.execute(sql, {"limit": limit})).all()
    return [row[0] for row in rows]


async def mark_dead(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    expected_state: TaskState,
    detail: str,
) -> Task | None:
    _assert_target(expected_state, TaskEvent.GIVE_UP, TaskState.DEAD)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'dead', last_error = :detail, finished_at = now(), updated_at = now()
        WHERE id = :task_id AND state = :expected_state
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {"task_id": task_id, "expected_state": expected_state.value, "detail": detail},
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def add_dead_letter(
    session: AsyncSession,
    *,
    task: Task,
    reason: str,
    detail: str | None,
) -> DeadLetter:
    letter = DeadLetter(
        task_id=task.id,
        reason=reason,
        detail=detail,
        reclaim_count=task.reclaim_count,
        retry_count=task.retry_count,
    )
    session.add(letter)
    await session.flush()
    return letter


async def resolve_dead_letters(
    session: AsyncSession, task_id: uuid.UUID, *, resolution: str
) -> int:
    rows = await session.execute(
        sa.text(
            """
            UPDATE dead_letter
            SET resolved_at = now(), resolution = :resolution
            WHERE task_id = :task_id AND resolved_at IS NULL
            RETURNING id
            """
        ),
        {"task_id": task_id, "resolution": resolution},
    )
    return len(rows.all())


async def list_dead_letters(
    session: AsyncSession,
    *,
    include_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[DeadLetter, str | None, int | None]]:
    stmt = (
        sa.select(DeadLetter, Task.title, Task.priority)
        .join(Task, Task.id == DeadLetter.task_id)
        .order_by(DeadLetter.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if not include_resolved:
        stmt = stmt.where(DeadLetter.resolved_at.is_(None))
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


# --------------------------------------------------------------------------- #
# 人工操作
# --------------------------------------------------------------------------- #
async def requeue(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    expected_states: Sequence[TaskState],
    reset_counters: bool,
) -> Task | None:
    """人工复活（死信/失败任务）。自动重试不走这里。"""
    _assert_target(TaskState.DEAD, TaskEvent.REQUEUE, TaskState.QUEUED)
    _assert_target(TaskState.FAILED, TaskEvent.REQUEUE, TaskState.QUEUED)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'queued',
            queued_at = now(),
            started_at = NULL,
            finished_at = NULL,
            lease_token = NULL,
            leased_until = NULL,
            heartbeat_at = NULL,
            next_retry_at = NULL,
            hold_reason = NULL,
            last_error = NULL,
            retry_count = CASE WHEN :reset_counters THEN 0 ELSE retry_count END,
            reclaim_count = CASE WHEN :reset_counters THEN 0 ELSE reclaim_count END,
            updated_at = now()
        WHERE id = :task_id
          AND state = ANY(CAST(:expected_states AS text[]))
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "expected_states": [s.value for s in expected_states],
                "reset_counters": reset_counters,
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def cancel(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    expected_states: Sequence[TaskState],
    reason: str,
) -> Task | None:
    _assert_target(TaskState.QUEUED, TaskEvent.CANCEL, TaskState.CANCELLED)
    _assert_target(TaskState.HELD, TaskEvent.CANCEL, TaskState.CANCELLED)
    _assert_target(TaskState.LEASED, TaskEvent.CANCEL, TaskState.CANCELLED)
    _assert_target(TaskState.RUNNING, TaskEvent.CANCEL, TaskState.CANCELLED)
    _assert_target(TaskState.FAILED, TaskEvent.CANCEL, TaskState.CANCELLED)
    _assert_target(TaskState.DEAD, TaskEvent.CANCEL, TaskState.CANCELLED)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'cancelled',
            cancel_reason = :reason,
            finished_at = now(),
            lease_token = NULL,
            leased_until = NULL,
            heartbeat_at = NULL,
            next_retry_at = NULL,
            updated_at = now()
        WHERE id = :task_id
          AND state = ANY(CAST(:expected_states AS text[]))
        RETURNING id
        """
    )
    row = (
        await session.execute(
            sql,
            {
                "task_id": task_id,
                "expected_states": [s.value for s in expected_states],
                "reason": reason,
            },
        )
    ).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def hold(session: AsyncSession, task_id: uuid.UUID, *, reason: str) -> Task | None:
    """`queued/held → held`。预算阀门在派活前把任务停下来，等人决策。"""
    _assert_target(TaskState.QUEUED, TaskEvent.HOLD, TaskState.HELD)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'held', hold_reason = :reason, updated_at = now()
        WHERE id = :task_id AND state IN ('queued', 'held')
        RETURNING id
        """
    )
    row = (await session.execute(sql, {"task_id": task_id, "reason": reason})).first()
    if row is None:
        return None
    return await _reload(session, task_id)


async def release(session: AsyncSession, task_id: uuid.UUID) -> Task | None:
    _assert_target(TaskState.HELD, TaskEvent.RELEASE, TaskState.QUEUED)
    sql = sa.text(
        """
        UPDATE tasks
        SET state = 'queued', hold_reason = NULL, queued_at = now(), updated_at = now()
        WHERE id = :task_id AND state = 'held'
        RETURNING id
        """
    )
    row = (await session.execute(sql, {"task_id": task_id})).first()
    if row is None:
        return None
    return await _reload(session, task_id)


# --------------------------------------------------------------------------- #
# 预算 / 设备统计 / 账本入口
# --------------------------------------------------------------------------- #
async def budget_totals(session: AsyncSession) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.text(
                """
                SELECT COUNT(*) FILTER (WHERE budget_cap IS NOT NULL) AS tasks_with_cap,
                       COUNT(*) FILTER (WHERE state = 'held')          AS tasks_held,
                       COALESCE(SUM(budget_cap), 0)                    AS cap_total,
                       COALESCE(SUM(cost_spent) FILTER (WHERE budget_cap IS NOT NULL), 0)
                                                                       AS spent_total
                FROM tasks
                """
            )
        )
    ).mappings().one()
    return dict(row)


async def record_cost(session: AsyncSession, task_id: uuid.UUID, amount: Decimal) -> None:
    """成本累加入口。**只给 W06/W07 的记账逻辑调用**，不对外暴露 HTTP。

    契约（plan/W03.md §5）：`tasks.cost_spent` 由记账方写，任务模块只读。
    """
    await session.execute(
        sa.text(
            """
            UPDATE tasks
            SET cost_spent = cost_spent + :amount, updated_at = now()
            WHERE id = :task_id
            """
        ),
        {"task_id": task_id, "amount": amount},
    )


async def get_device_stats(session: AsyncSession, device_id: str) -> TaskDeviceStats | None:
    return await session.get(TaskDeviceStats, device_id)


async def touch_device_stats(
    session: AsyncSession, device_id: str, *, consecutive_high: int
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO task_device_stats (device_id, consecutive_high, last_claimed_at, updated_at)
            VALUES (:device_id, :consecutive_high, now(), now())
            ON CONFLICT (device_id) DO UPDATE
            SET consecutive_high = :consecutive_high,
                last_claimed_at = now(),
                updated_at = now()
            """
        ),
        {"device_id": device_id, "consecutive_high": consecutive_high},
    )
