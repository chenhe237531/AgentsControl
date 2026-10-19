"""能力上限标记与立项校验（W08-I03）。

这个模块回答的不是"哪个模型最强"，而是**"这一步现在能不能做"**。
它决定你是否该对一个听起来很好的目标说"先别立项"。

三条设计约束（对应 plan/W08.md 的验收）：

1. **判定看"最好的模型"**：某维度全模型都低于 70% → `blocked`。
   只要有一个模型能过线，就说明"能做，只是挑模型"——那是成本问题，不是能力问题。
   反过来，"全模型都不行"是不能靠换模型解决的，所以��用上限的度量必须是 max，不是均值：
   均值会被一个跑崩的模型拖下去，让你误判整个方向不可行。
2. **三档而不是两档**：`blocked`（< 70）/ `risky`（70–80）/ `ok`（≥ 80）。
   80 这条线是"能托付给自动化"的门槛，70 是"做了也是白做"的门槛。
   中间地带最危险——看起来能跑，实际上每三次错一次。
3. **建议必须指向"砍环节"而不是"换模型"**（验收 ③）：
   全模型都 < 80% 时，换一个更贵的模型通常只是把 75 分变成 77 分；
   真正有效的是拆小任务、把开放式判断改成规则、或者干脆砍掉这个环节。
   因此 `advice.model_swap_would_help` 恒为 False，建议列表里**不允许**出现"换模型"。

阈值判定是纯函数（`evaluate_dimension`），不读库、不读系统时间，边界（69.9 / 70.1 / 79.9）可被单测钉死。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .radar import DIMENSION_LABELS, WeekWindow, aggregate_radar
from .schemas import DIMENSIONS

#: 全模型上限低于此分 → 该维度被判 `blocked`（做了也是白做）
BLOCKED_BELOW = 70.0
#: 全模型上限低于此分 → 该维度被判 `risky`（能跑但不稳，不能托付）
RISKY_BELOW = 80.0

CeilingStatus = str  # ok / risky / blocked / insufficient（Literal 定义在 schemas）

#: 建议模板。`model_swap_would_help` 恒为 False：这条字段存在的意义是**显式否认**
#: "换模型能解决"这个直觉，让 W02 在立项界面上没法把它读成"再买个贵模型就行"。
ADVICE: dict[str, dict[str, object]] = {
    "blocked": {
        "action": "cut",
        "headline": "全模型都做不到，换模型救不了",
        "model_swap_would_help": False,
        "options": [
            "拆小：把该维度的任务拆成 2–3 个更窄的子任务，只做能做的部分",
            "换规则：把开放式判断改成结构化检查（模板 / 校验规则 / 人工兜底）",
            "砍掉：本季度不立项，这个环节继续由人来做",
        ],
    },
    "risky": {
        "action": "shrink",
        "headline": "能做但不稳，不要整段托付给自动化",
        "model_swap_would_help": False,
        "options": [
            "拆小：先只自动化最确定的那一段，其余保留人工确认",
            "换规则：把「看起来对不对」改成可判定的检查项（字段齐全 / 引用可追溯）",
            "加闸门：产出必须过人审才能继续，别让它自动往下走",
        ],
    },
    "ok": {
        "action": "go",
        "headline": "可以立项，按常规闸门走",
        "model_swap_would_help": False,
        "options": ["按计划立项；上线后仍要盯漂移（W08-I04）"],
    },
    "insufficient": {
        "action": "add_tasks",
        "headline": "还没测过，结论不可用",
        "model_swap_would_help": False,
        "options": ["先补题：该维度题量不足，任何结论都是猜的"],
    },
}


@dataclass(frozen=True, slots=True)
class CeilingEntry:
    """单个维度的能力上限。

    `best_score` 是**最好的那个模型**的分数（不是均值）。`status=insufficient`
    时它是 None——注意不要写成 0，0 会被读成"这个维度很差"，从而误砍一个方向。
    """

    dimension: str
    label: str
    status: CeilingStatus
    best_model: str | None = None
    best_score: float | None = None
    samples: int = 0
    per_model: dict[str, float | None] = field(default_factory=dict)

    @property
    def threshold(self) -> float:
        """该维度"能托付"的分数线。blocked 与 risky 的判定都以此为锚。"""
        return RISKY_BELOW

    @property
    def gap(self) -> float | None:
        """距 80 分线还差多少（负数表示已过线）。给 W02 展示"差 11.6 分"用。"""
        if self.best_score is None:
            return None
        return round(RISKY_BELOW - self.best_score, 2)


def evaluate_dimension(
    dimension: str,
    *,
    per_model: dict[str, float | None],
    samples: int = 0,
) -> CeilingEntry:
    """纯函数：按分数上限判 blocked / risky / ok。

    边界语义（验收 ④）：69.9 → blocked，70.1 → risky，79.9 → risky，80.0 → ok。
    即 `blocked` 是 `best < 70`，`risky` 是 `70 <= best < 80`，左闭右开。
    """
    scored = {m: s for m, s in per_model.items() if s is not None}
    if not scored:
        return CeilingEntry(
            dimension=dimension,
            label=DIMENSION_LABELS.get(dimension, dimension),
            status="insufficient",
            samples=samples,
            per_model=per_model,
        )

    # 并列最高分时取模型名最小的，保证同一批数据两次调用给同样的答案（可测、可复现）
    best_model = min(scored, key=lambda m: (-float(scored[m] or 0.0), m))
    best_score = float(scored[best_model] or 0.0)

    if best_score < BLOCKED_BELOW:
        status = "blocked"
    elif best_score < RISKY_BELOW:
        status = "risky"
    else:
        status = "ok"

    return CeilingEntry(
        dimension=dimension,
        label=DIMENSION_LABELS.get(dimension, dimension),
        status=status,
        best_model=best_model,
        best_score=round(best_score, 2),
        samples=samples,
        per_model=per_model,
    )


def build_ceiling(
    samples: list[object],
    *,
    window: WeekWindow,
    models: list[str] | None = None,
) -> dict[str, object]:
    """把一周的样本转成 8 维能力上限。

    复用 `aggregate_radar()` 取分层分数，不重复实现聚合：雷达和上限看的是同一份数据，
    两套聚合迟早会对不上（雷达说 82，立项校验说 79）。
    """
    radar = aggregate_radar(samples, window=window, models=models)

    dims: list[dict[str, object]] = []
    blocked: list[str] = []
    risky: list[str] = []
    insufficient: list[str] = []

    for item in radar["dimensions"]:  # type: ignore[index]
        per_model: dict[str, float | None] = {
            str(model): (None if block["score"] is None else float(block["score"]))
            for model, block in (item["models"] or {}).items()  # type: ignore[union-attr]
        }
        ceiling = evaluate_dimension(
            str(item["dimension"]),
            per_model=per_model,
            samples=int(item["samples"]),  # type: ignore[arg-type]
        )
        dims.append(
            {
                "dimension": ceiling.dimension,
                "label": ceiling.label,
                "status": ceiling.status,
                "best_model": ceiling.best_model,
                "best_score": ceiling.best_score,
                "samples": ceiling.samples,
                "models": ceiling.per_model,
                "threshold": ceiling.threshold,
                "gap": ceiling.gap,
                "advice": ADVICE[ceiling.status],
            }
        )
        if ceiling.status == "blocked":
            blocked.append(ceiling.dimension)
        elif ceiling.status == "risky":
            risky.append(ceiling.dimension)
        elif ceiling.status == "insufficient":
            insufficient.append(ceiling.dimension)

    return {
        "week": radar["week"],
        "week_start": radar["week_start"],
        "week_end": radar["week_end"],
        "models": radar["models"],
        "samples": radar["samples"],
        "thresholds": {"blocked_below": BLOCKED_BELOW, "risky_below": RISKY_BELOW},
        "dimensions": dims,
        "blocked_dimensions": blocked,
        "risky_dimensions": risky,
        "insufficient_dimensions": insufficient,
    }


def _format_dim(entry: dict[str, object]) -> str:
    """`截图与视觉 68.4（m1 最好）`——立项界面上要能直接引用这句话。"""
    score = entry.get("best_score")
    if score is None:
        return f"{entry['label']}（无数据）"
    return f"{entry['label']} {float(score):.1f}（{entry['best_model']} 最好）"


def check_capability(
    ceiling: dict[str, object],
    *,
    requested: list[str] | None = None,
) -> dict[str, object]:
    """立项校验：给定要做的维度，返回能不能做、卡在哪、具体差多少分。

    `requested` 为空表示"按整张雷达判断"（适合周会/看板）；
    传了就只判这几维——W02 立项时关心的是自己那一步，不是全部 8 维。
    """
    all_dims = {str(d["dimension"]): d for d in ceiling["dimensions"]}  # type: ignore[index]
    unknown = [d for d in (requested or []) if d not in all_dims]
    if unknown:
        # 未知维度不能静默忽略：立项校验最怕"查了个不存在的维度，返回 ok"
        from app.core.errors import ValidationError

        raise ValidationError(
            f"未知维度：{', '.join(unknown)}",
            details={"unknown_dimensions": unknown, "known_dimensions": list(DIMENSIONS)},
        )

    picked = [all_dims[d] for d in (requested or list(all_dims))]

    blockers = [e for e in picked if e["status"] == "blocked"]
    unreliable = [e for e in picked if e["status"] == "risky"]
    unknown_dims = [e for e in picked if e["status"] == "insufficient"]

    if blockers:
        verdict = "blocked"
    elif unreliable:
        verdict = "risky"
    elif unknown_dims:
        # 有维度没测过就别给 ok：那等于用"没数据"担保"能做"
        verdict = "insufficient"
    else:
        verdict = "ok"

    # 摘要必须点名具体维度 + 得分（验收 ②），不能只说"可能不行"
    if blockers:
        summary = "不可立项：" + "；".join(_format_dim(e) for e in blockers)
    elif unreliable:
        summary = "可做但要缩范围：" + "；".join(_format_dim(e) for e in unreliable)
    elif unknown_dims:
        summary = "结论不可用（缺数据）：" + "、".join(str(e["label"]) for e in unknown_dims)
    else:
        summary = "可以立项：" + "；".join(_format_dim(e) for e in picked)

    return {
        "verdict": verdict,
        "week": ceiling["week"],
        "requested": [str(e["dimension"]) for e in picked],
        "checked": [
            {
                "dimension": e["dimension"],
                "label": e["label"],
                "status": e["status"],
                "best_model": e["best_model"],
                "best_score": e["best_score"],
                "samples": e["samples"],
                "threshold": e["threshold"],
                "gap": e["gap"],
                "advice": e["advice"],
            }
            for e in picked
        ],
        "blockers": [_format_dim(e) for e in blockers],
        "unreliable": [_format_dim(e) for e in unreliable],
        "unknown": [str(e["dimension"]) for e in unknown_dims],
        "summary": summary,
    }


__all__ = [
    "ADVICE",
    "BLOCKED_BELOW",
    "RISKY_BELOW",
    "CeilingEntry",
    "build_ceiling",
    "check_capability",
    "evaluate_dimension",
]
