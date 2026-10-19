"""任务队列运行参数（本模块私有配置）。

为什么不用 `app.core.config.settings`：
`backend/app/core/**` 是冻结文件（owner W01），本窗口不能直接加字段。
已按 `x-shared-registry` 约定提交「给 core 增加 TASK_* 配置项」的变更请求；
在 W01 合并之前，本文件从同名环境变量读取，语义与未来 core 字段完全一致，
届时本文件退化为薄适配层（只保留 `TaskSettings.from_core()`）。

硬约束（W03 坑 #2）：
**租约必须显著大于心跳间隔**。默认心跳 30 s、租约 90 s，即容忍丢 2 次心跳。
若 `lease <= heartbeat`，一次网络抖动就会让正常任务被回收，表现为「任务反复重启」，
因此这里在加载时直接 fail fast，而不是留到线上才发现。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - 配置错误直接失败
        raise ValueError(f"{name} 必须是整数，实际为 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"{name} 必须是数字，实际为 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class TaskSettings:
    # --- 租约 ---
    lease_seconds: int = 90
    heartbeat_seconds: int = 30
    lease_min_seconds: int = 30
    lease_max_seconds: int = 3600
    reclaim_max: int = 3
    reclaim_batch: int = 200

    # --- 优先级与公平 ---
    priority_min: int = 0
    priority_max: int = 9
    high_priority_max: int = 3
    low_priority_min: int = 7
    fairness_burst: int = 5
    queued_overdue_seconds: int = 900

    # --- 预算 ---
    budget_reserve_ratio: float = 0.10

    # --- 重试 ---
    retry_max: int = 3
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.heartbeat_seconds < 5:
            problems.append("TASK_HEARTBEAT_SECONDS 必须 >= 5")
        if self.lease_seconds < self.heartbeat_seconds * 2:
            problems.append(
                "TASK_LEASE_SECONDS 必须 >= 2 × TASK_HEARTBEAT_SECONDS"
                f"（当前 {self.lease_seconds} vs {self.heartbeat_seconds}）"
            )
        if self.lease_min_seconds > self.lease_seconds:
            problems.append("TASK_LEASE_MIN_SECONDS 不能大于 TASK_LEASE_SECONDS")
        if not 0 <= self.high_priority_max < self.low_priority_min <= self.priority_max:
            problems.append("优先级分档必须满足 0 <= high_max < low_min <= max")
        if not 0.0 <= self.budget_reserve_ratio < 1.0:
            problems.append("TASK_BUDGET_RESERVE_RATIO 必须在 [0, 1) 内")
        if self.fairness_burst < 2:
            problems.append("TASK_FAIRNESS_BURST 必须 >= 2（否则公平调度没有意义）")
        if self.retry_max < 0:
            problems.append("TASK_RETRY_MAX 不能为负")
        if problems:
            raise ValueError("任务队列配置非法: " + "; ".join(problems))

    @classmethod
    def from_env(cls) -> TaskSettings:
        return cls(
            lease_seconds=_env_int("TASK_LEASE_SECONDS", 90),
            heartbeat_seconds=_env_int("TASK_HEARTBEAT_SECONDS", 30),
            lease_min_seconds=_env_int("TASK_LEASE_MIN_SECONDS", 30),
            lease_max_seconds=_env_int("TASK_LEASE_MAX_SECONDS", 3600),
            reclaim_max=_env_int("TASK_RECLAIM_MAX", 3),
            reclaim_batch=_env_int("TASK_RECLAIM_BATCH", 200),
            priority_min=_env_int("TASK_PRIORITY_MIN", 0),
            priority_max=_env_int("TASK_PRIORITY_MAX", 9),
            high_priority_max=_env_int("TASK_HIGH_PRIORITY_MAX", 3),
            low_priority_min=_env_int("TASK_LOW_PRIORITY_MIN", 7),
            fairness_burst=_env_int("TASK_FAIRNESS_BURST", 5),
            queued_overdue_seconds=_env_int("TASK_QUEUED_OVERDUE_SECONDS", 900),
            budget_reserve_ratio=_env_float("TASK_BUDGET_RESERVE_RATIO", 0.10),
            retry_max=_env_int("TASK_RETRY_MAX", 3),
            retry_base_seconds=_env_int("TASK_RETRY_BASE_SECONDS", 30),
            retry_max_seconds=_env_int("TASK_RETRY_MAX_SECONDS", 3600),
        )

    def clamp_lease(self, requested: int | None) -> int:
        """工作端可申请更长的租约，但不允许低于下限或高于上限。"""
        if requested is None:
            return self.lease_seconds
        return max(self.lease_min_seconds, min(int(requested), self.lease_max_seconds))


@lru_cache(maxsize=1)
def get_task_settings() -> TaskSettings:
    return TaskSettings.from_env()


def reload_task_settings() -> TaskSettings:
    """测试用：清缓存后重新读取环境变量。"""
    get_task_settings.cache_clear()
    return get_task_settings()


task_settings = get_task_settings()
