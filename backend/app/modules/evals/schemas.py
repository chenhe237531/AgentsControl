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


# ---------------------------------------------------------------------------
# 能力上限与立项校验（W08-I03）：W02 立项界面直接引用这里的字段
# ---------------------------------------------------------------------------

#: `insufficient` 是刻意加的第四档：没测过 ≠ 做不到，也 ≠ 能做
CeilingStatus = Literal["ok", "risky", "blocked", "insufficient"]

#: 立项结论。`insufficient` 表示"有维度没数据，不能给你 ok"
CeilingVerdict = Literal["ok", "risky", "blocked", "insufficient"]


class CeilingAdvice(BaseModel):
    """建议。`model_swap_would_help` 恒为 False——这是对"换模型就行"的显式否认。"""

    action: Literal["go", "shrink", "cut", "add_tasks"]
    headline: str
    model_swap_would_help: bool = False
    options: list[str] = Field(default_factory=list)


class DimensionCeiling(BaseModel):
    """单个维度的能力上限。`best_score` 是**最好的模型**的分数，不是均值。"""

    dimension: str
    label: str
    status: CeilingStatus
    best_model: str | None = None
    best_score: float | None = None
    samples: int = 0
    models: dict[str, float | None] = Field(default_factory=dict)
    threshold: float
    #: 距 `threshold` 的差距（负数=已过线）。W02 用它显示"差 11.6 分"
    gap: float | None = None
    advice: CeilingAdvice


class CeilingOut(BaseModel):
    week: str
    week_start: date
    week_end: date
    models: list[str] = Field(default_factory=list)
    samples: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
    dimensions: list[DimensionCeiling] = Field(default_factory=list)
    blocked_dimensions: list[str] = Field(default_factory=list)
    risky_dimensions: list[str] = Field(default_factory=list)
    insufficient_dimensions: list[str] = Field(default_factory=list)


class CeilingCheckRequest(BaseModel):
    """立项校验入参。`dimensions` 留空 = 按整张雷达判断。

    `dimensions` 刻意用 `list[str]` 而不是 `list[Dimension]`：Literal 校验只会回
    "Input should be 'long_form'..."，而立项界面需要看到"你写的 telepathy 不认识，
    合法值是这 8 个"。未知维度在 `ceiling.check_capability()` 里显式拒绝。
    """

    dimensions: list[str] | None = Field(
        default=None, max_length=8, description="留空表示检查全部 8 维"
    )
    week: str | None = Field(default=None, max_length=10)
    models: list[str] | None = None


class CeilingCheckItem(BaseModel):
    dimension: str
    label: str
    status: CeilingStatus
    best_model: str | None = None
    best_score: float | None = None
    samples: int = 0
    threshold: float
    gap: float | None = None
    advice: CeilingAdvice


class CeilingCheckOut(BaseModel):
    verdict: CeilingVerdict
    week: str
    requested: list[str] = Field(default_factory=list)
    checked: list[CeilingCheckItem] = Field(default_factory=list)
    #: 点名具体维度 + 得分，例如 "截图与视觉 68.4（m1 最好）"
    blockers: list[str] = Field(default_factory=list)
    unreliable: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    summary: str


__all__ = [
    "DIMENSIONS",
    "CeilingAdvice",
    "CeilingCheckItem",
    "CeilingCheckOut",
    "CeilingCheckRequest",
    "CeilingOut",
    "CeilingStatus",
    "CeilingVerdict",
    "Dimension",
    "DimensionCeiling",
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
