"""带宽熔断 —— 纯函数，不查库（便于穷举单测）。

四条规则（plan/W07.md I04）：
1. 日决策数 > 20         → 非高风险全部转明日；
2. 连续 3 天决策耗时上升 50% → 派活量降 30%；
3. 否决率 10% → 40%      → 标记"可能疲劳"，抽 3 条要求复核；
4. 23:00–07:00 的 L0 决策 → 只记录"待明日确认"，不给结论。

阈值全部来自配置（环境变量，AFC_BREAKER_*），可改不需要发版。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    # 规则 1：日决策数上限
    daily_decision_limit: int = 20
    # 规则 2：连续天数 / 累计涨幅阈值 / 派活量削减
    duration_window_days: int = 3
    duration_rise_pct: Decimal = Decimal("0.5")
    dispatch_cut_pct: Decimal = Decimal("0.3")
    # 规则 3：否决率基线（正常水平）与疲劳阈值
    veto_floor_pct: Decimal = Decimal("0.10")
    veto_ceiling_pct: Decimal = Decimal("0.40")
    review_sample: int = 3
    # 规则 4：静默窗口（按用户本地时区换算后的整点）
    night_start_hour: int = 23
    night_end_hour: int = 7
    # 用户的本地时区偏移（小时），默认 UTC+8
    night_tz_offset_hours: int = 8


ENV_PREFIX = "AFC_BREAKER_"


def load_breaker_config() -> BreakerConfig:
    """从环境变量读阈值 —— 阈值是配置不是代码。"""
    overrides: dict[str, object] = {}

    def _int(name: str) -> int | None:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return None
        return int(raw)

    def _decimal(name: str) -> Decimal | None:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return None
        return Decimal(raw)

    mapping: dict[str, str] = {
        "daily_decision_limit": "DAILY_DECISION_LIMIT",
        "duration_window_days": "DURATION_WINDOW_DAYS",
        "dispatch_cut_pct": "DISPATCH_CUT_PCT",
        "review_sample": "REVIEW_SAMPLE",
        "night_start_hour": "NIGHT_START_HOUR",
        "night_end_hour": "NIGHT_END_HOUR",
        "night_tz_offset_hours": "NIGHT_TZ_OFFSET_HOURS",
    }
    for attr, suffix in mapping.items():
        value = _int(f"{ENV_PREFIX}{suffix}")
        if value is not None:
            overrides[attr] = value
    for attr, suffix in {
        "duration_rise_pct": "DURATION_RISE_PCT",
        "veto_floor_pct": "VETO_FLOOR_PCT",
        "veto_ceiling_pct": "VETO_CEILING_PCT",
    }.items():
        value = _decimal(f"{ENV_PREFIX}{suffix}")
        if value is not None:
            overrides[attr] = value
    valid = {f.name for f in fields(BreakerConfig)}
    return BreakerConfig(**{k: v for k, v in overrides.items() if k in valid})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BreakerInput:
    """调用方（service）负责把数据库事实压成这个快照。"""

    decisions_today: int
    # 每日平均决策耗时（分钟），旧 → 新，至少 3 天才能触发规则 2
    daily_avg_minutes: tuple[float, ...] = ()
    # 最近周期否决率 与 基线周期否决率（如最近 7 天 vs 之前 7 天）
    veto_rate_recent: Decimal = Decimal("0")
    veto_rate_baseline: Decimal = Decimal("0")
    # 用户本地时间的当前小时（service 用 night_tz_offset_hours 换算）
    now_hour: int = 12


@dataclass(frozen=True, slots=True)
class BreakerVerdict:
    defer_non_critical: bool = False
    dispatch_factor: Decimal = Decimal("1.0")
    fatigue_suspected: bool = False
    review_sample: int = 0
    night_hold_l0: bool = False
    reasons: tuple[str, ...] = field(default=())


def local_hour(utc_hour: int, tz_offset_hours: int) -> int:
    return (utc_hour + tz_offset_hours) % 24


def is_night(hour: int, cfg: BreakerConfig) -> bool:
    """静默窗口跨午夜：[23, 24) ∪ [0, 7)。"""
    if cfg.night_start_hour <= cfg.night_end_hour:
        return cfg.night_start_hour <= hour < cfg.night_end_hour
    return hour >= cfg.night_start_hour or hour < cfg.night_end_hour


def _duration_rising(values: tuple[float, ...], cfg: BreakerConfig) -> bool:
    """连续 N 天上升且累计涨幅 ≥ 阈值（50%）。

    判定：取最近 N 天，逐日严格递增，且最新一天 ≥ 最早一天 × (1 + rise_pct)。
    """
    window = values[-cfg.duration_window_days :]
    if len(window) < cfg.duration_window_days or cfg.duration_window_days < 2:
        return False
    for prev, curr in zip(window, window[1:], strict=False):
        if curr <= prev:
            return False
    rise = Decimal(str(window[-1])) / Decimal(str(window[0])) - Decimal(1)
    return rise >= cfg.duration_rise_pct


def evaluate(inp: BreakerInput, cfg: BreakerConfig | None = None) -> BreakerVerdict:
    cfg = cfg or BreakerConfig()
    reasons: list[str] = []
    defer = inp.decisions_today > cfg.daily_decision_limit
    if defer:
        reasons.append(
            f"daily_decisions_{inp.decisions_today}_gt_{cfg.daily_decision_limit}"
        )
    cut = _duration_rising(inp.daily_avg_minutes, cfg)
    dispatch_factor = Decimal("1.0") - cfg.dispatch_cut_pct if cut else Decimal("1.0")
    if cut:
        reasons.append("decision_duration_rising")
    fatigue = (
        inp.veto_rate_baseline <= cfg.veto_floor_pct
        and inp.veto_rate_recent >= cfg.veto_ceiling_pct
    )
    if fatigue:
        reasons.append("veto_rate_spike")
    night = is_night(inp.now_hour, cfg)
    if night:
        reasons.append("night_window_l0_hold")
    return BreakerVerdict(
        defer_non_critical=defer,
        dispatch_factor=dispatch_factor,
        fatigue_suspected=fatigue,
        review_sample=cfg.review_sample if fatigue else 0,
        night_hold_l0=night,
        reasons=tuple(reasons),
    )
