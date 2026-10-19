"""goals 模块状态机与编号规则（纯函数，零 IO）。

放在独立文件而不是 service.py，是因为它们：
- 纯函数（输入输出明确）；
- 可穷举单测覆盖所有迁移与边界；
- 多处复用（service 转状态、router 校验、summary 聚合）。

状态机：
    draft → active → {achieved, frozen, degraded}

非法迁移（含反向、跳级、终态出口）一律返回 False，service 抛 ConflictError 409。
衰减（degraded）只用于「进化轨 T+7 无产出」或主动放弃，赚钱轨不做衰减。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

Track = Literal["money", "growth"]
GoalStatus = Literal["draft", "active", "achieved", "frozen", "degraded"]
GrowthStopLossWindow = timedelta(days=7)
MoneyStopLossDefaultP25 = 0.0  # 历史 P25 占位，等 W07 接入账本后由调用方传入


# ---------------------------------------------------------------------------
# 状态转移：纯函数。返回是否允许。
# ---------------------------------------------------------------------------


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active"},
    "active": {"achieved", "frozen", "degraded"},
    "achieved": set(),
    "frozen": set(),
    "degraded": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: str, target: str) -> None:
    """不允许即抛 ConflictError。service 直接调用以便传播 HTTP 状态码。"""
    from app.core.errors import ConflictError

    if not can_transition(current, target):
        raise ConflictError(
            f"非法状态迁移：{current} → {target}",
        )


# ---------------------------------------------------------------------------
# 目标编号 O-{M|E}-YYYYMMDD-NNN
# ---------------------------------------------------------------------------


def track_prefix(track: str) -> str:
    """money → M，growth → E。其它抛 ValueError。"""
    if track == "money":
        return "M"
    if track == "growth":
        return "E"
    raise ValueError(f"unknown track: {track}")


def build_code(track: str, date: datetime, seq: int) -> str:
    """格式：O-{M|E}-YYYYMMDD-NNN（NNN 补 0，最少 3 位）。"""
    return f"O-{track_prefix(track)}-{date.strftime('%Y%m%d')}-{seq:03d}"


# ---------------------------------------------------------------------------
# 止损判定（纯函数）—— W02-I04 落地
# ---------------------------------------------------------------------------


def is_growth_stop_loss_triggered(
    *,
    active_at: datetime,
    now: datetime,
    has_deliverable: bool,
) -> bool:
    """进化轨纯函数：T+7 内无任何产出物 → 触发降级。

    - has_deliverable 表示目标已有可复用认知资产（卡片/解题路径/清单）；
    - 不为它生成待办（待办在 W03）。
    """
    if has_deliverable:
        return False
    return now >= active_at + GrowthStopLossWindow


def is_money_stop_loss_triggered(
    *,
    history_p25: float,
    current_output: float,
) -> bool:
    """赚钱轨纯函数：当前产出 < 历史 P25 → 触发冻结。

    current_output / history_p25 计算"产出低于历史下四分位"。
    """
    return current_output < history_p25


__all__ = [
    "Track",
    "GoalStatus",
    "GrowthStopLossWindow",
    "can_transition",
    "assert_transition",
    "track_prefix",
    "build_code",
    "is_growth_stop_loss_triggered",
    "is_money_stop_loss_triggered",
]