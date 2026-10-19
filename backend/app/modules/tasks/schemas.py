"""任务模块的 HTTP 契约（请求/响应模型）。

只做「形状」校验，业务规则在 service / machine / matcher / valve 里。
字段命名对前端与设备端直接可见，改名即 breaking change（见 `CONTRACT.md`）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .machine import TaskState
from .settings import TaskSettings, task_settings


class TaskRequires(BaseModel):
    """调度能力声明。**键名是跨端契约**（W04/W06 复用），拼错等于不匹配。"""

    model_config = ConfigDict(extra="forbid")

    gui: bool = False
    orca: bool = False
    browser: bool = False
    always_on: bool = False


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    goal_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    requires: TaskRequires = Field(default_factory=TaskRequires)
    priority: int = Field(default=5, ge=0, le=9)
    budget_cap: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    max_retries: int = Field(default=3, ge=0, le=10)
    retryable: bool = True

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title 不能为空白")
        return v


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID | None
    title: str
    payload: dict[str, Any]
    requires: dict[str, Any]
    priority: int
    state: TaskState
    device_id: str | None
    leased_until: datetime | None
    heartbeat_at: datetime | None
    reclaim_count: int
    retry_count: int
    max_retries: int
    retryable: bool
    next_retry_at: datetime | None
    last_error: str | None
    budget_cap: Decimal | None
    cost_spent: Decimal
    hold_reason: str | None
    result: dict[str, Any] | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # 派生字段：排队超时告警（W03-I05），由服务端算，客户端不参与
    queued_seconds: int = 0
    queued_overdue: bool = False

    @classmethod
    def from_task(cls, task: Any, *, now: datetime, settings: TaskSettings | None = None) -> TaskOut:
        cfg = settings or task_settings
        age = max(0, int((now - task.queued_at).total_seconds()))
        overdue = (
            task.state == TaskState.QUEUED.value and age >= cfg.queued_overdue_seconds
        )
        model = cls.model_validate(task)
        return model.model_copy(update={"queued_seconds": age, "queued_overdue": overdue})


class TaskListOut(BaseModel):
    items: list[TaskOut]
    count: int
    limit: int
    offset: int


class ClaimIn(BaseModel):
    """工作端领取下一个可执行任务。

    `caps` 必填（可以为空对象）：服务端**只按设备自报能力派活**。
    不传 = 不声明能力 → 只会领到无能力要求的任务，避免「静默错派」。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128)
    caps: dict[str, bool] = Field(default_factory=dict)
    lease_seconds: int | None = Field(default=None, ge=5, le=3600)


class LeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128)
    caps: dict[str, bool] = Field(default_factory=dict)
    lease_seconds: int | None = Field(default=None, ge=5, le=3600)


class LeaseOut(BaseModel):
    """租约凭证。`lease_token` 只在领取响应里出现一次，之后靠它续约与交货。"""

    task: TaskOut
    lease_token: uuid.UUID
    leased_until: datetime
    heartbeat_seconds: int
    lease_seconds: int


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID


class HeartbeatOut(BaseModel):
    task_id: uuid.UUID
    leased_until: datetime
    heartbeat_seconds: int


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    result: dict[str, Any] | None = None


class FailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: uuid.UUID
    error: str = Field(default="unknown", max_length=2000)
    #: None = 沿用任务的 `retryable`；显式传值可把「确定性失败」直接判死
    retryable: bool | None = None


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="owner_cancelled", max_length=500)


class RequeueIn(BaseModel):
    """人工复活死信。自动重试永远不经过这里（W03-I06 验收 ②）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    reset_counters: bool = True


class CandidateOut(BaseModel):
    device_id: str
    name: str | None = None
    eligible: bool
    reasons: list[str] = Field(default_factory=list)


class CandidatesOut(BaseModel):
    task_id: uuid.UUID
    candidates: list[CandidateOut]
    #: 设备目录尚未落地 / 全部不可派时，用一句话说明，前端直接展示
    note: str | None = None


class DeadLetterOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    reason: str
    detail: str | None
    reclaim_count: int
    retry_count: int
    created_at: datetime
    resolved_at: datetime | None
    resolution: str | None
    task_title: str | None = None
    task_priority: int | None = None


class DeadLetterListOut(BaseModel):
    items: list[DeadLetterOut]
    count: int


class BudgetStatusOut(BaseModel):
    """预算总览。只读预聚合值（`tasks.cost_spent`），不在请求里扫账本明细。"""

    tasks_with_cap: int
    tasks_held: int
    cap_total: Decimal
    spent_total: Decimal
    reserve_ratio: float
    available_total: Decimal
