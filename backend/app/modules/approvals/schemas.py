"""闸门模块的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["L0", "L1", "L2", "L3"]
Decision = Literal["approve", "reject"]
Status = Literal["pending", "decided"]
DecisionValue = Literal["approved", "rejected"]


class ApprovalCreate(BaseModel):
    level: Level
    title: str = Field(min_length=1, max_length=256)
    payload: dict[str, object] | None = None
    task_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    level: Level
    title: str
    payload: dict[str, object] | None
    task_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    status: Status
    visible_at: datetime
    created_at: datetime
    decided_at: datetime | None
    decision: DecisionValue | None
    decider: str | None
    reason: str | None
    held_reason: str | None
    held_at: datetime | None


class ApprovalCreateOut(BaseModel):
    approval: ApprovalOut
    deferred: bool  # 日限额超了 → 自动转明日


class ApprovalQueueOut(BaseModel):
    items: list[ApprovalOut]
    count: int


class DecideIn(BaseModel):
    decision: Decision
    # reason 可为空但必须留字段 —— 事后复盘靠它（plan/W07.md I03 验收 ③）
    reason: str | None = Field(default=None, max_length=2000)
    # 客户端测得的决策耗时（分钟），计入注意力账本（source=gate）
    minutes: int | None = Field(default=None, ge=1, le=1440)


class DecideOut(BaseModel):
    approval: ApprovalOut
    # True = L0 在静默窗口被决策：只记录"待明日确认"，不给结论
    held: bool


class BreakerConfigOut(BaseModel):
    daily_decision_limit: int
    duration_window_days: int
    duration_rise_pct: Decimal
    dispatch_cut_pct: Decimal
    veto_floor_pct: Decimal
    veto_ceiling_pct: Decimal
    review_sample: int
    night_start_hour: int
    night_end_hour: int
    night_tz_offset_hours: int


class BreakerInputOut(BaseModel):
    decisions_today: int
    daily_avg_minutes: list[float]
    veto_rate_recent: Decimal
    veto_rate_baseline: Decimal
    now_hour: int


class BreakerVerdictOut(BaseModel):
    defer_non_critical: bool
    dispatch_factor: Decimal
    fatigue_suspected: bool
    review_sample: int
    night_hold_l0: bool
    reasons: list[str]


class BandwidthOut(BaseModel):
    config: BreakerConfigOut
    input: BreakerInputOut
    verdict: BreakerVerdictOut
