"""能力匹配：任务 `requires` × 设备 `caps` → 可派 / 不可派 + 原因。

规则只有一处（`CAPABILITY_RULES`），调度器（SQL 过滤）与候选列表（HTTP）共用同一份定义。
不匹配就不派——尤其是 `always_on` 任务绝不派给会休眠的笔记本，
那是「任务莫名卡住」的头号原因（plan/W03.md §2.4）。

`reasons` 是给人看的中文短句，前端直接展示：
「这台机器会休眠」比「无匹配设备」有用得多（W03-I03 验收 ②）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    #: `tasks.requires` 中的键
    requires_key: str
    #: 设备 `caps` 中的键
    caps_key: str
    #: 不满足时的展示文案
    reason: str
    #: True 表示「requires=True 时要求 caps_key 为 False」（always_on × can_sleep）
    inverted: bool = False

    def satisfied(self, requires: Mapping[str, Any], caps: Mapping[str, Any]) -> bool:
        if not bool(requires.get(self.requires_key, False)):
            return True
        value = bool(caps.get(self.caps_key, False))
        return (not value) if self.inverted else value


#: 能力矩阵（docs/TECH-ARCHITECTURE-v1.md §4.4）的可执行版本。
CAPABILITY_RULES: Final[tuple[CapabilityRule, ...]] = (
    CapabilityRule("gui", "has_gui", "设备没有图形界面，需要人在旁边的任务不能派给它"),
    CapabilityRule("orca", "has_orca", "该设备未安装 Orca，无法执行 Orca 任务"),
    CapabilityRule("browser", "has_browser", "该设备没有可用的浏览器自动化"),
    CapabilityRule(
        "always_on",
        "can_sleep",
        "这台机器会休眠，不能承接常驻任务",
        inverted=True,
    ),
)

#: 能力矩阵里的设备画像，供 `/candidates` 的测试与文档使用（也便于 UI 预览）。
DEVICE_PROFILES: Final[dict[str, dict[str, bool]]] = {
    "always_on_mac": {  # 常用 Mac 主机
        "has_gui": True,
        "has_orca": True,
        "has_browser": True,
        "has_docker": True,
        "can_sleep": False,
    },
    "remote_mac_mini": {  # 远程 Mac mini
        "has_gui": True,
        "has_orca": True,
        "has_browser": True,
        "has_docker": True,
        "can_sleep": False,
    },
    "laptop": {  # Win/Mac 笔记本
        "has_gui": True,
        "has_orca": True,
        "has_browser": True,
        "has_docker": False,
        "can_sleep": True,
    },
    "vps": {  # 云服务器
        "has_gui": False,
        "has_orca": False,
        "has_browser": True,  # 无头浏览器
        "has_docker": True,
        "can_sleep": False,
    },
    "third_party": {  # 第三方平台：不是 Agent 宿主，只提供纯 API 能力
        "has_gui": False,
        "has_orca": False,
        "has_browser": False,
        "has_docker": False,
        "can_sleep": True,
    },
}


@dataclass(frozen=True, slots=True)
class MatchResult:
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


def evaluate(requires: Mapping[str, Any] | None, caps: Mapping[str, Any] | None) -> MatchResult:
    """纯函数：不查库、不读配置，输入相同则输出相同。"""
    req = requires or {}
    capabilities = caps or {}
    reasons = tuple(
        rule.reason for rule in CAPABILITY_RULES if not rule.satisfied(req, capabilities)
    )
    return MatchResult(eligible=not reasons, reasons=reasons)


def is_eligible(requires: Mapping[str, Any] | None, caps: Mapping[str, Any] | None) -> bool:
    return evaluate(requires, caps).eligible


def explain(requires: Mapping[str, Any] | None, caps: Mapping[str, Any] | None) -> str:
    result = evaluate(requires, caps)
    return "可派" if result.eligible else "；".join(result.reasons)


def rank(
    candidates: list[tuple[str, str | None, Mapping[str, Any]]],
    requires: Mapping[str, Any] | None,
) -> list[tuple[str, str | None, MatchResult]]:
    """给候选设备排序：可派的在前，同等条件下按名字稳定排序（不引入随机性）。"""
    scored = [
        (device_id, name, evaluate(requires, caps)) for device_id, name, caps in candidates
    ]
    scored.sort(key=lambda item: (not item[2].eligible, item[1] or item[0]))
    return scored


def requires_sql_conditions(alias: str = "t") -> tuple[str, ...]:
    """把同一套规则翻译成 SQL 谓词，供 `claim_next` 在单条语句里过滤。

    规则必须与 `CAPABILITY_RULES` 一一对应（`test_matcher_sql_matches_rules` 校验），
    否则会出现「候选列表说不可派、队列却派了」的两套真相。
    """
    return (
        f"(COALESCE(({alias}.requires->>'gui')::boolean, false) = false OR :has_gui)",
        f"(COALESCE(({alias}.requires->>'orca')::boolean, false) = false OR :has_orca)",
        f"(COALESCE(({alias}.requires->>'browser')::boolean, false) = false OR :has_browser)",
        f"(COALESCE(({alias}.requires->>'always_on')::boolean, false) = false OR :can_sleep = false)",
    )
