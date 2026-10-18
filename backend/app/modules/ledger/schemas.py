"""账本模块的请求/响应模型。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["subscription", "tools", "device", "rework", "qc", "labor"]
PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class LedgerEntryCreate(BaseModel):
    """成本归集入参。金额非 0（负数=冲正），且必须挂 task 或 goal。"""

    category: Category
    amount: Decimal
    currency: str = Field(default="CNY", max_length=8)
    occurred_at: datetime | None = None
    task_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    memo: str | None = None
    source: str = Field(min_length=1, max_length=32)
    source_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _must_be_traceable_and_nonzero(self) -> LedgerEntryCreate:
        if self.task_id is None and self.goal_id is None:
            raise ValueError("task_id 与 goal_id 至少提供一个（拒绝无主成本）")
        if self.amount == 0:
            raise ValueError("amount 不允许为 0（无信息量；冲正请用负数）")
        return self


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: datetime
    category: Category
    amount: Decimal
    currency: str
    task_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    memo: str | None
    source: str
    source_id: str
    created_at: datetime


class LedgerEntryListOut(BaseModel):
    items: list[LedgerEntryOut]
    count: int


class AttentionStatsOut(BaseModel):
    """min/outcome：期内注意力分钟 ÷ 达标产出数。无产出时为 null，不是 0。"""

    period: str
    minutes: Decimal
    outcomes: int | None
    min_per_outcome: Decimal | None


class LedgerSummaryOut(BaseModel):
    """周期内六分项 + 注意力指标。缺失项返回 null / 0，不报错。"""

    period: str
    currency: str
    categories: dict[str, Decimal]
    total: Decimal
    attention: AttentionStatsOut


class BudgetStatusOut(BaseModel):
    goal_id: uuid.UUID
    budget: Decimal | None
    spent: Decimal
    remaining: Decimal | None
    frozen: bool


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    monthly_fee: Decimal = Field(gt=0)
    quota_limit: int | None = Field(default=None, ge=1)
    amortize_mode: Literal["calendar", "usage"] = "calendar"
    started_on: date


class SubscriptionUpdate(BaseModel):
    quota_used: int | None = Field(default=None, ge=0)
    active: bool | None = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    monthly_fee: Decimal
    quota_limit: int | None
    quota_used: int
    amortize_mode: Literal["calendar", "usage"]
    started_on: date
    active: bool
    created_at: datetime
    # 计算指标（当期）
    amortized_current_period: Decimal | None = None
    quota_consumption: Decimal | None = None
    quota_alert: bool = False


class SubscriptionListOut(BaseModel):
    items: list[SubscriptionOut]
    count: int


class ReconcileRowOut(BaseModel):
    source: str
    ledger_amount: Decimal | None
    source_amount: Decimal | None
    diff: Decimal | None
    diff_pct: Decimal | None
    flagged: bool


class ReconcileReportOut(BaseModel):
    period: str
    threshold_pct: Decimal
    dry_run: bool
    consistent: bool
    rows: list[ReconcileRowOut]
