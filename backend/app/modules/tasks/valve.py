"""预算阀门：派活前的保守拦截。

原则（W03-I04 验收）：
1. **宁可少派**：预留 `budget_reserve_ratio`（默认 10%）余量，
   只有 `已花 + 预留 + 预计花费 <= 上限` 才放行；
2. **不在这里算账**：只读 `tasks.cost_spent` 这个预聚合值。
   账本明细属于 W07，在请求处理器里扫明细会把派活延迟拖成秒级；
3. 规则是纯函数，便于穷举单测；
4. 超预算的任务**不进入 queued**，进 `held` 并给出人能看懂的原因。

SQL 侧还有一个等价谓词 `LEASE_BUDGET_PREDICATE`，供 `claim_next` 在单条语句里过滤，
避免「先领到再退回」的抖动；两边一致性由 `test_valve_sql_predicate_matches_python` 守护。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reason: str | None
    cap: Decimal | None
    spent: Decimal
    reserved: Decimal
    available: Decimal

    @property
    def hold_reason(self) -> str | None:
        return None if self.allowed else self.reason


def _money(value: Decimal | float | int | str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _fmt(value: Decimal) -> str:
    """统一展示为两位小数，避免 ``12.3400`` 这种账本精度泄漏到 UI 文案里。"""
    quant = value.quantize(Decimal("0.01"))
    return f"{quant}"


def evaluate(
    *,
    cost_spent: Decimal | float | int | str | None,
    budget_cap: Decimal | float | int | str | None,
    reserve_ratio: float,
    projected_cost: Decimal | float | int | str | None = 0,
) -> BudgetDecision:
    """预算判定（纯函数）。

    - `budget_cap is None` → 不限额，直接放行；
    - 预留额 = `cap * reserve_ratio`，永远不可动用于新任务；
    - 预计花费默认为 0：派活时我们只知道「已经花了多少」，不预测未来。
    """
    cap = _money(budget_cap)
    spent = _money(cost_spent) or ZERO
    projected = _money(projected_cost) or ZERO

    if cap is None:
        return BudgetDecision(
            allowed=True,
            reason=None,
            cap=None,
            spent=spent,
            reserved=ZERO,
            available=ZERO,
        )

    reserved = (cap * Decimal(str(reserve_ratio))).quantize(Decimal("0.0001"))
    available = cap - spent - reserved
    # 保守判定：可用额度必须严格为正，才允许再派活。
    # `spent == cap`、`cap == 0`、只剩预留额时一律拦下（宁可少派）。
    allowed = available > 0 and projected <= available
    reason = None
    if not allowed:
        reason = (
            f"预算不足：已花 {_fmt(spent)} / 上限 {_fmt(cap)}，"
            f"预留 {_fmt(reserved)}（{int(reserve_ratio * 100)}%）后可用 {_fmt(max(available, ZERO))}，"
            f"本次预计还需 {_fmt(projected)}"
        )
    return BudgetDecision(
        allowed=allowed,
        reason=reason,
        cap=cap,
        spent=spent,
        reserved=reserved,
        available=available,
    )


#: 与 `evaluate(..., projected_cost=0).allowed` 等价的 SQL 谓词。
#: 用 `tasks` 表的真实列名，直接拼进 `claim_next` 的 WHERE。
LEASE_BUDGET_PREDICATE: Final[str] = (
    "(t.budget_cap IS NULL"
    " OR t.cost_spent + (t.budget_cap * :budget_reserve_ratio) < t.budget_cap)"
)


def available_total(
    *,
    cap_total: Decimal | float | int | str | None,
    spent_total: Decimal | float | int | str | None,
    reserve_ratio: float,
) -> Decimal:
    cap = _money(cap_total) or ZERO
    spent = _money(spent_total) or ZERO
    reserved = (cap * Decimal(str(reserve_ratio))).quantize(Decimal("0.0001"))
    return max(cap - spent - reserved, ZERO)
