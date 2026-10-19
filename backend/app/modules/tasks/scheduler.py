"""调度策略：优先级分档、防饿死插队、重试退避。

全部是纯函数——调度是「任务莫名卡住 / 高优任务把低优任务饿死」这类事故的源头，
必须能脱离数据库穷举验证。
"""

from __future__ import annotations

from typing import Final, Literal

PriorityBand = Literal["high", "normal", "low"]

HIGH: Final[PriorityBand] = "high"
NORMAL: Final[PriorityBand] = "normal"
LOW: Final[PriorityBand] = "low"


def clamp_priority(value: int, *, minimum: int = 0, maximum: int = 9) -> int:
    return max(minimum, min(int(value), maximum))


def band(priority: int, *, high_max: int = 3, low_min: int = 7) -> PriorityBand:
    """0–3 高优，4–6 普通，7–9 低优（数字越小越优先）。"""
    if priority <= high_max:
        return HIGH
    if priority >= low_min:
        return LOW
    return NORMAL


def should_force_low(consecutive_high: int, burst: int) -> bool:
    """某设备连续领取高优任务达到 `burst` 次后，强制插队一个低优任务。

    这是「公平」的全部实现：不做加权轮询、不维护虚拟时间，只保证
    **低优任务最多等 N 个高优任务就能被领走**。复杂度可控，行为可解释。
    """
    return consecutive_high >= burst


def next_consecutive_high(
    current: int, priority: int, *, high_max: int = 3, low_min: int = 7
) -> int:
    """领取一个任务之后，设备的高优连击计数如何变化。

    - 高优任务：+1（继续累积）
    - 低优任务：归零（插队已经发生，公平性得到满足）
    - 普通任务：保持不变（既不加剧饥饿，也不解除）
    """
    b = band(priority, high_max=high_max, low_min=low_min)
    if b == HIGH:
        return current + 1
    if b == LOW:
        return 0
    return current


def backoff_seconds(
    attempt: int, *, base_seconds: int = 30, max_seconds: int = 3600
) -> int:
    """指数退避：第 1 次重试等 base，第 2 次 2×base，第 3 次 4×base，封顶。

    `attempt` 从 1 开始（表示「第几次重试」）。刻意不加随机抖动：
    任务量级小，抖动只会让测试与线上排查变难。
    """
    if attempt <= 0:
        return 0
    exponent = min(attempt - 1, 20)  # 防止 2**n 溢出
    return min(base_seconds * (2**exponent), max_seconds)


def is_retry_exhausted(attempts_used: int, max_retries: int) -> bool:
    """重试次数是否已耗尽。`attempts_used` 含本次失败。"""
    return attempts_used > max_retries
