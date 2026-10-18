"""goals 模块 Pydantic 契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Track = Literal["money", "growth"]
TemplateId = Literal["A", "B", "C", "D", "E", "F", "G", "H", ""]
GoalStatus = Literal["draft", "active", "achieved", "frozen", "degraded"]


class GoalCreate(BaseModel):
    """POST /goals 请求体。

    - `track` 决定预算字段：money → budget_money，growth → budget_hours
    - `template` 可选，给出时 `defaults` 可填，避免三问重答
    - `intake_item_id` 可选，从 Inbox 转化而来时关联
    """

    track: Track
    template: TemplateId = ""
    title: str = Field(..., min_length=1, max_length=300)
    acceptance: str = Field(..., min_length=1, max_length=2000)
    horizon: str | None = Field(default=None, max_length=64)
    budget_money: float | None = Field(default=None, ge=0)
    budget_hours: float | None = Field(default=None, ge=0)
    attention_minutes_per_day: int | None = Field(default=None, ge=1)
    north_star: str | None = None
    guardrails: dict[str, Any] | None = None
    deliverables: dict[str, Any] | None = None
    intake_item_id: str | None = None

    @field_validator("acceptance")
    @classmethod
    def _acceptance_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("acceptance 不能为空")
        return v


class GoalPatch(BaseModel):
    """PATCH /goals/{id} 请求体。

    任意字段可选；目标转换状态用 `status` 字段。
    不允许修改 code / owner_subject / created_at。
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    acceptance: str | None = Field(default=None, min_length=1, max_length=2000)
    horizon: str | None = None
    horizon_at: datetime | None = None
    budget_money: float | None = Field(default=None, ge=0)
    budget_hours: float | None = Field(default=None, ge=0)
    attention_minutes_per_day: int | None = Field(default=None, ge=1)
    north_star: str | None = None
    guardrails: dict[str, Any] | None = None
    deliverables: dict[str, Any] | None = None
    status: GoalStatus | None = None
    stop_loss_triggered_at: datetime | None = None


class GoalOut(BaseModel):
    id: str
    code: str
    owner_subject: str
    track: Track
    template: TemplateId
    title: str
    acceptance: str
    horizon: str | None
    horizon_at: datetime | None
    budget_money: float | None
    budget_hours: float | None
    attention_minutes_per_day: int | None
    north_star: str | None
    guardrails: dict[str, Any] | None
    deliverables: dict[str, Any] | None
    status: GoalStatus
    intake_item_id: str | None
    created_at: datetime
    updated_at: datetime
    achieved_at: datetime | None = None
    frozen_at: datetime | None = None
    degraded_at: datetime | None = None
    stop_loss_triggered_at: datetime | None = None

    model_config = {"from_attributes": True}


class StopLossResult(BaseModel):
    goal_id: str
    triggered: bool
    reason: str | None = None


class GoalSummaryItem(BaseModel):
    """GET /goals/summary 单条目。"""

    id: str
    code: str
    title: str
    track: Track
    status: GoalStatus
    budget_used: float | None = Field(
        default=None,
        description="已用预算。money 轨是 ¥，growth 轨是小时。unknown=数据源未就绪。",
    )
    budget_remaining: float | None = Field(default=None)
    days_remaining: int | None = Field(
        default=None,
        description="距离 TTL 还有几天。无 TTL 时为 null。"
    )
    stop_loss_triggered: bool
    stop_loss_reason: str | None = None


class GoalSummary(BaseModel):
    items: list[GoalSummaryItem]
    total: int


__all__ = [
    "Track",
    "TemplateId",
    "GoalStatus",
    "GoalCreate",
    "GoalPatch",
    "GoalOut",
    "StopLossResult",
    "GoalSummaryItem",
    "GoalSummary",
]