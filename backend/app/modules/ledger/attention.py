"""注意力账本纯函数 —— min/outcome 口径。

达标产出的唯一定义（plan/W07.md I02 ③）：
  目标 `achieved`，或 任务 `done` 且验收通过。
数据源是 W02（goals）/ W03（tasks）的表，落地前 `count_outcomes` 返回 None，
min/outcome 相应返回 null —— 缺失返回 null，不是 0（0 会把无产出期伪装成高效期）。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.errors import ValidationError

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


def month_bounds(period: str) -> tuple[datetime, datetime]:
    """'YYYY-MM' → [当月第一天 00:00Z, 下月第一天 00:00Z)。

    跨月边界刻意用半开区间：月末最后一秒属于本月，下月第一秒属于下月。
    """
    m = _PERIOD_RE.match(period.strip())
    if not m:
        raise ValidationError(f"period 必须形如 YYYY-MM，收到 {period!r}")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValidationError(f"month 非法: {month}")
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def current_period(today: date) -> str:
    return f"{today:%Y-%m}"


def next_month(period: str) -> str:
    start, end = month_bounds(period)
    return f"{end.date():%Y-%m}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def compute_min_per_outcome(minutes: Decimal, outcomes: int | None) -> Decimal | None:
    """min/outcome = 期内注意力分钟 ÷ 达标产出数。

    无产出（None 或 0）→ None（前端显示 '—'，绝不能显示 0）。
    """
    if outcomes is None or outcomes <= 0:
        return None
    return (Decimal(minutes) / Decimal(outcomes)).quantize(Decimal("0.01"))
