"""任务状态机 —— **全模块唯一的迁移真相源**。

设计约束（W03-I01 验收）：
1. 状态与迁移全部写在本文件的 `TRANSITIONS` 字典里，**任何模块不得直接 UPDATE state**；
   仓储层的每条写 SQL 都把「期望状态」写进 WHERE，并在执行前用 `next_state()` 断言目标状态，
   规则一旦漂移会立刻抛错，而不是静默写脏数据。
2. 非法迁移统一抛 `ConflictError`（HTTP 409），客户端据此提示「刷新后重试」。
3. 纯函数：无 IO、无时间依赖，因此可以用一张矩阵穷举测试。

状态图（与 plan/W03.md 一致）::

    queued ──lease──> leased ──start──> running ──complete──> done
      │                 │                 │
      │                 ├──reclaim────────┴──> queued
      │                 ├──reclaim_exhausted──> dead
      │                 └──fail──> failed ──retry/requeue──> queued
      │                                └──give_up──> dead
      ├──hold──> held ──release──> queued
      └──cancel──> cancelled（除 done 外任意非终态均可取消；dead 也可取消）

`held` 是预算阀门的产物（W03-I04）：**超预算的任务不进入 queued**，停在这里等人处理。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from app.core.errors import ConflictError


class TaskState(StrEnum):
    QUEUED = "queued"
    HELD = "held"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class TaskEvent(StrEnum):
    LEASE = "lease"
    START = "start"
    HEARTBEAT = "heartbeat"
    COMPLETE = "complete"
    FAIL = "fail"
    RECLAIM = "reclaim"
    RECLAIM_EXHAUSTED = "reclaim_exhausted"
    HOLD = "hold"
    RELEASE = "release"
    RETRY = "retry"
    REQUEUE = "requeue"
    GIVE_UP = "give_up"
    CANCEL = "cancel"


#: 状态 × 事件 → 目标状态。缺失的组合即「非法迁移」。
TRANSITIONS: Final[dict[TaskState, dict[TaskEvent, TaskState]]] = {
    TaskState.QUEUED: {
        TaskEvent.LEASE: TaskState.LEASED,
        TaskEvent.HOLD: TaskState.HELD,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.HELD: {
        TaskEvent.RELEASE: TaskState.QUEUED,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.LEASED: {
        TaskEvent.START: TaskState.RUNNING,
        # 心跳是自环：任务状态不变，只延长租约
        TaskEvent.HEARTBEAT: TaskState.LEASED,
        # 短任务允许跳过 start 直接交货
        TaskEvent.COMPLETE: TaskState.DONE,
        TaskEvent.FAIL: TaskState.FAILED,
        TaskEvent.RECLAIM: TaskState.QUEUED,
        TaskEvent.RECLAIM_EXHAUSTED: TaskState.DEAD,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.RUNNING: {
        TaskEvent.HEARTBEAT: TaskState.RUNNING,
        TaskEvent.COMPLETE: TaskState.DONE,
        TaskEvent.FAIL: TaskState.FAILED,
        TaskEvent.RECLAIM: TaskState.QUEUED,
        TaskEvent.RECLAIM_EXHAUSTED: TaskState.DEAD,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.FAILED: {
        TaskEvent.RETRY: TaskState.QUEUED,
        TaskEvent.REQUEUE: TaskState.QUEUED,
        TaskEvent.GIVE_UP: TaskState.DEAD,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.DEAD: {
        # 死信只能由人工 POST /tasks/{id}/requeue 复活，不自动
        TaskEvent.REQUEUE: TaskState.QUEUED,
        TaskEvent.CANCEL: TaskState.CANCELLED,
    },
    TaskState.DONE: {},
    TaskState.CANCELLED: {},
}

#: 终态：不再接受任何事件（cancelled 可从 dead 进入，但 cancelled 自身是终点）。
TERMINAL_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.DONE, TaskState.CANCELLED}
)

#: 持有租约的状态。回收与心跳只认这两个状态。
LEASED_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.LEASED, TaskState.RUNNING}
)

#: 排队中可被领取的状态。
CLAIMABLE_STATES: Final[frozenset[TaskState]] = frozenset({TaskState.QUEUED})


def all_states() -> tuple[TaskState, ...]:
    return tuple(TaskState)


def all_events() -> tuple[TaskEvent, ...]:
    return tuple(TaskEvent)


def allowed_events(state: TaskState | str) -> tuple[TaskEvent, ...]:
    return tuple(TRANSITIONS[TaskState(state)])


def can(state: TaskState | str, event: TaskEvent | str) -> bool:
    return TaskEvent(event) in TRANSITIONS[TaskState(state)]


def is_terminal(state: TaskState | str) -> bool:
    return TaskState(state) in TERMINAL_STATES


def next_state(state: TaskState | str, event: TaskEvent | str) -> TaskState:
    """返回目标状态；非法迁移抛 409。

    这是唯一允许「决定任务下一步状态」的函数。服务层只允许：
    ``to_state = machine.next_state(row.state, event)`` → 交给仓储层原子写入。
    """
    current = TaskState(state)
    evt = TaskEvent(event)
    target = TRANSITIONS[current].get(evt)
    if target is None:
        allowed = [e.value for e in TRANSITIONS[current]]
        raise ConflictError(
            f"任务状态 {current.value} 不接受事件 {evt.value}",
            code="illegal_transition",
            details={"state": current.value, "event": evt.value, "allowed_events": allowed},
        )
    return target


def matrix() -> list[dict[str, str]]:
    """导出矩阵，供测试与文档核对（穷举状态 × 事件）。"""
    rows: list[dict[str, str]] = []
    for state in all_states():
        for event in all_events():
            target = TRANSITIONS[state].get(event)
            rows.append(
                {
                    "state": state.value,
                    "event": event.value,
                    "next": target.value if target else "",
                }
            )
    return rows
