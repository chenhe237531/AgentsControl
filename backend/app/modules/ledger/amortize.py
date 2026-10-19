"""订阅摊销与配额消耗率 —— 纯函数。

订阅制下边际成本≈0，CPO 里的"模型 token"一项改为三指标
（INTEGRATION-ORCA.md §6.1）：
  1. 订阅摊销（本文件）：按自然月天数均摊，可切换按实际用量；
  2. 配额消耗率（本文件）：已用 / 周期额度，>80% 告警；
  3. 复核分钟（min/outcome，见 attention.py）。
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

CENTS = Decimal("0.0001")  # 与 NUMERIC(14,4) 对齐
QUOTA_ALERT_THRESHOLD = Decimal("0.80")  # > 80% 告警（严格大于）


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_of(d: date) -> tuple[int, int]:
    return d.year, d.month


def next_month_of(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prorate_month(monthly_fee: Decimal, year: int, month: int, active_days: int) -> Decimal:
    """按自然月天数均摊：月中第 N 天生效 → 该月摊 fee × active_days / 当月天数。"""
    dim = days_in_month(year, month)
    active_days = max(0, min(active_days, dim))
    return (Decimal(monthly_fee) * Decimal(active_days) / Decimal(dim)).quantize(CENTS)


def amortize_calendar(
    monthly_fee: Decimal,
    started_on: date,
    period_start: date,
    period_end: date,
) -> Decimal:
    """日历摊销：period 为 [period_start, period_end)。

    - 生效月：按剩余天数（含生效当天）折算；
    - 之后每个完整覆盖的自然月：整月费用；
    - period 跨月时逐月累加（"跨月摊销"的口径就在这里）。
    """
    total = Decimal("0.0000")
    y, m = month_of(period_start)
    while (y, m) < month_of(period_end):
        if (y, m) < month_of(started_on):
            amount = Decimal("0.0000")
        elif (y, m) == month_of(started_on):
            active_days = days_in_month(y, m) - started_on.day + 1
            amount = prorate_month(monthly_fee, y, m, active_days)
        else:
            amount = Decimal(monthly_fee).quantize(CENTS)
        total += amount
        y, m = next_month_of(y, m)
    return total


def amortize_usage(
    monthly_fee: Decimal, quota_used: int, quota_limit: int | None
) -> Decimal:
    """用量摊销：按实际用量占比分摊当月费用（占比封顶 100%）。"""
    if quota_limit is None or quota_limit <= 0:
        return Decimal(monthly_fee).quantize(CENTS)
    ratio = min(Decimal(quota_used) / Decimal(quota_limit), Decimal(1))
    return (Decimal(monthly_fee) * ratio).quantize(CENTS)


def quota_consumption(quota_used: int, quota_limit: int | None) -> Decimal | None:
    """配额消耗率 ∈ [0, ∞)，无配额限制时为 null。"""
    if quota_limit is None or quota_limit <= 0:
        return None
    return (Decimal(quota_used) / Decimal(quota_limit)).quantize(Decimal("0.0001"))


def quota_alert(consumption: Decimal | None, threshold: Decimal = QUOTA_ALERT_THRESHOLD) -> bool:
    """配额消耗率 > 阈值（默认 80%）→ 告警。等于阈值不告警。"""
    if consumption is None:
        return False
    return consumption > threshold
