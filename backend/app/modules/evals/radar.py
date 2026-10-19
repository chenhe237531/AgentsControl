"""8 维评分与雷达聚合（W08-I02）。

设计要点（对应 plan/W08.md 的验收）：

1. **纯函数**：`aggregate_radar()` 只接收样本列表，不读数据库、不读系统时间，
   所有边界（空维度、单样本、偶数样本的 p50）都能被单测钉死。
2. **缺样本返回 `insufficient` 而不是 0**：0 分会被下游（立项校验、雷达图）
   误读成"这个维度很差"，而真相往往是"这个维度还没测"。两者决策后果相反：
   前者让你砍掉一个能力，后者只让你去补题。
3. **成本按样本摊薄**：缓存命中的结果 `cost_usd=0` 但分数有效，
   因此 `cost = 本周总花费 / 样本数`（摊薄成本），不是"非缓存样本的均价"——
   后者会让"缓存用得好"看起来反而更贵。

一周的边界是 ISO 周（周一 00:00 UTC 起，到下周一 00:00 止）。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.core.errors import ValidationError

from .schemas import DIMENSIONS

#: 维度 → 中文名（W10 画雷达图的轴标签，也是 `insufficient` 时给用户的提示）
DIMENSION_LABELS: dict[str, str] = {
    "long_form": "长文写作",
    "code": "代码",
    "extraction": "结构化抽取",
    "tool_call": "多轮工具调用",
    "vision": "截图与视觉",
    "long_context": "长上下文",
    "zh_nuance": "中文语感",
    "instruction": "指令遵循",
}

#: 一个维度少于这个样本数就算"证据不足"。1 是刻意的：
#: 只要真跑过一次，就应该给出分数（哪怕置信度低），而不是永远 insufficient。
MIN_SAMPLES = 1


@dataclass(frozen=True, slots=True)
class WeekWindow:
    """ISO 周窗口。`end` 是**开区间**（下周一 00:00），便于 SQL 用 `< end`。"""

    label: str
    start: datetime
    end: datetime

    @property
    def last_day(self) -> date:
        """窗口最后一天（含），给前端展示用。"""
        return (self.end - timedelta(days=1)).date()


@dataclass(frozen=True, slots=True)
class RadarSample:
    """聚合的输入单位：一次"某模型在某维度的某道题"的得分。"""

    dimension: str
    model: str
    score: float
    cost_usd: float
    latency_ms: int
    cached: bool = False


def _window_from_iso(year: int, week: int) -> WeekWindow:
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ValidationError(
            f"无效的 ISO 周：{year}-W{week}",
            details={"week": f"{year}-W{week:02d}"},
        ) from exc
    start = datetime(monday.year, monday.month, monday.day)
    return WeekWindow(
        label=f"{monday.isocalendar().year}-W{monday.isocalendar().week:02d}",
        start=start,
        end=start + timedelta(days=7),
    )


def parse_week(spec: str | None, *, now: datetime) -> WeekWindow:
    """解析 `week` 参数。

    接受三种写法（放宽输入，但**只输出** `YYYY-Www` 一种标签）：
    - 省略 / 空串 → 当前 ISO 周（按 `now`）
    - `2026-W42` / `2026W42` → 该 ISO 周
    - `2026-10-14` → 该日期所在的 ISO 周（人更容易记住日期）
    """
    if spec is None or not spec.strip():
        iso = now.isocalendar()
        return _window_from_iso(iso.year, iso.week)

    # 归一化：2026W42 → 2026-W42；2026-W42 保持不变
    raw = spec.strip().upper().replace("W", "-W").replace("--", "-")

    # 2026-W42
    if "-W" in raw:
        year_part, _, week_part = raw.partition("-W")
        if not (year_part.isdigit() and week_part.isdigit()):
            raise ValidationError(
                "week 格式应为 YYYY-Www（如 2026-W42）或 YYYY-MM-DD",
                details={"week": spec},
            )
        return _window_from_iso(int(year_part), int(week_part))

    # 2026-10-14
    try:
        day = date.fromisoformat(spec.strip())
    except ValueError as exc:
        raise ValidationError(
            "week 格式应为 YYYY-Www（如 2026-W42）或 YYYY-MM-DD",
            details={"week": spec},
        ) from exc
    iso = day.isocalendar()
    return _window_from_iso(iso.year, iso.week)


def _p50(values: list[int]) -> int:
    """p50（中位数）。偶数样本取中间两个的均值再四舍五入。"""
    return int(round(statistics.median(values)))


def _model_block(
    rows: list[RadarSample],
) -> dict[str, dict[str, object]]:
    """单模型单维度的聚合块。空 rows 返回 insufficient（score=None）。"""
    if len(rows) < MIN_SAMPLES:
        return {
            "status": "insufficient",
            "score": None,
            "cost": 0.0,
            "p50_latency": None,
            "samples": 0,
        }
    samples = len(rows)
    return {
        "status": "ok",
        "score": round(sum(r.score for r in rows) / samples, 2),
        "cost": round(sum(r.cost_usd for r in rows) / samples, 6),
        "p50_latency": _p50([r.latency_ms for r in rows]),
        "samples": samples,
    }


def aggregate_radar(
    samples: list[RadarSample],
    *,
    window: WeekWindow,
    models: list[str] | None = None,
) -> dict[str, object]:
    """把样本聚合成 8 维雷达。

    `models` 显式传入时，即使某模型某维没有样本也会出现在 `models` 里
    （status=insufficient）——雷达图需要完整的轴，缺的轴是"没测"，不是"零分"。
    """
    by_dimension: dict[str, list[RadarSample]] = {d: [] for d in DIMENSIONS}
    # 显式指定模型 = 只关心这几个模型：先把别人的样本剔除，否则 total 会被别人撑大，
    # 而轴名却只有被指定的几个，读的人会以为"分数算错了"。
    if models:
        wanted = set(models)
        samples = [s for s in samples if s.model in wanted]
        model_order = sorted(wanted)
    else:
        model_order = sorted({s.model for s in samples})

    for sample in samples:
        # 未知维度（历史数据或题目迁移残留）直接丢弃，不让它污染 8 轴；
        # 但通过 `unknown_dimensions` 暴露出来，避免"静默丢数据"。
        if sample.dimension in by_dimension:
            by_dimension[sample.dimension].append(sample)

    dimensions: list[dict[str, object]] = []
    for dimension in DIMENSIONS:
        rows = by_dimension[dimension]
        block = _model_block(rows)
        per_model: dict[str, dict[str, object]] = {}
        for model in model_order:
            per_model[model] = _model_block([r for r in rows if r.model == model])
        dimensions.append(
            {
                "dimension": dimension,
                "label": DIMENSION_LABELS.get(dimension, dimension),
                **block,
                "models": per_model,
            }
        )

    total_samples = sum(1 for s in samples if s.dimension in by_dimension)
    unknown = sorted({s.dimension for s in samples if s.dimension not in by_dimension})
    return {
        "week": window.label,
        "week_start": window.start.date(),
        "week_end": window.last_day,
        "models": model_order,
        "samples": total_samples,
        "total_cost_usd": round(sum(s.cost_usd for s in samples), 6),
        "dimensions": dimensions,
        "unknown_dimensions": unknown,
    }


__all__ = [
    "DIMENSION_LABELS",
    "MIN_SAMPLES",
    "RadarSample",
    "WeekWindow",
    "aggregate_radar",
    "parse_week",
]
