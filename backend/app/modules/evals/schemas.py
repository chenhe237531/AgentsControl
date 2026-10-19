"""evals 模块的 Pydantic 契约。

字段名是稳定契约（W10 据此画雷达图，W02 立项时读 ceiling），**不可随意重命名**。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Dimension = Literal[
    "long_form",  # 长文写作
    "code",  # 代码
    "extraction",  # 结构化抽取
    "tool_call",  # 多轮工具调用
    "vision",  # 截图与视觉
    "long_context",  # 长上下文
    "zh_nuance",  # 中文语感
    "instruction",  # 指令遵循
]

DIMENSIONS: tuple[str, ...] = (
    "long_form",
    "code",
    "extraction",
    "tool_call",
    "vision",
    "long_context",
    "zh_nuance",
    "instruction",
)

RunStatus = Literal["queued", "running", "succeeded", "failed", "aborted"]


class RubricItem(BaseModel):
    """评分细则的一条。weight 之和不必等于 1，服务端只做展示与提示。"""

    criterion: str = Field(..., min_length=1, max_length=200)
    weight: float = Field(default=1.0, ge=0, le=10)


class TaskCreate(BaseModel):
    dimension: Dimension
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=8000)
    rubric: list[RubricItem] = Field(default_factory=list)
    max_score: float = Field(default=100, gt=0, le=100)
    weight: float = Field(default=1, gt=0, le=10)


class TaskUpdate(BaseModel):
    """局部更新：字段为 None 表示不改。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    rubric: list[RubricItem] | None = None
    max_score: float | None = Field(default=None, gt=0, le=100)
    weight: float | None = Field(default=None, gt=0, le=10)
    dimension: Dimension | None = None
    active: bool | None = None


class TaskOut(BaseModel):
    id: str
    dimension: str
    title: str
    prompt: str
    rubric: list[dict] = Field(default_factory=list)
    max_score: float
    weight: float
    active: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TaskList(BaseModel):
    items: list[TaskOut]
    total: int
    per_dimension: dict[str, int] = Field(default_factory=dict)


class SeedResult(BaseModel):
    """题库导入结果。`per_dimension` 让"8 维各 ≥2 题"这条验收可被直接断言。"""

    created: int
    skipped: int
    total: int
    per_dimension: dict[str, int]
    valid: bool
    problems: list[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    models: list[str] = Field(..., min_length=1, max_length=10)
    task_ids: list[str] | None = Field(
        default=None, description="留空表示跑全部 active 题目"
    )
    provider: str = Field(default="fake", max_length=32)
    cost_cap_cny: float | None = Field(default=None, gt=0, le=10000)


class RunOut(BaseModel):
    id: str
    status: RunStatus
    provider: str
    models: list[str]
    task_count: int
    result_count: int
    cache_hits: int
    cost_usd: float
    cost_cap_cny: float
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class ResultOut(BaseModel):
    task_id: str
    model: str
    score: float
    cost_usd: float
    latency_ms: int
    raw_ref: str
    cached: bool

    model_config = {"from_attributes": True}


class RunDetail(BaseModel):
    run: RunOut
    results: list[ResultOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 雷达（W08-I02）：字段名是 W10 画图的契约
# ---------------------------------------------------------------------------

RadarStatus = Literal["ok", "insufficient"]


class ModelDimensionScore(BaseModel):
    """某模型在某维度上的表现。`insufficient` 时 `score` 为 None 而不是 0。"""

    status: RadarStatus
    score: float | None = None
    cost: float = 0.0
    p50_latency: int | None = None
    samples: int = 0


class DimensionScore(BaseModel):
    """一个维度的聚合结果。`models` 是同一维度的按模型拆分（雷达图每条轴要分层画）。"""

    dimension: str
    label: str
    status: RadarStatus
    score: float | None = None
    cost: float = 0.0
    p50_latency: int | None = None
    samples: int = 0
    models: dict[str, ModelDimensionScore] = Field(default_factory=dict)


class RadarOut(BaseModel):
    week: str
    week_start: date
    week_end: date
    models: list[str] = Field(default_factory=list)
    samples: int = 0
    total_cost_usd: float = 0.0
    dimensions: list[DimensionScore] = Field(default_factory=list)
    #: 落在 8 轴之外的维度名（历史残留），显式暴露而不是静默丢弃
    unknown_dimensions: list[str] = Field(default_factory=list)


__all__ = [
    "DIMENSIONS",
    "Dimension",
    "DimensionScore",
    "ModelDimensionScore",
    "RadarOut",
    "RadarStatus",
    "RubricItem",
    "RunCreate",
    "RunDetail",
    "RunOut",
    "RunStatus",
    "ResultOut",
    "SeedResult",
    "TaskCreate",
    "TaskList",
    "TaskOut",
    "TaskUpdate",
]
