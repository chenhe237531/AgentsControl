"""账本对账 —— 纯函数。

对账语义：账本合计（按 source 分组）vs 来源事件合计。
差异 > 阈值（默认 1%）标红并给出差值明细；
人为制造 0.5% 差异应通过、2% 差异应失败（验收口径即测试用例）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

DEFAULT_THRESHOLD_PCT = Decimal("0.01")  # 1%
ZERO = Decimal("0.0000")


@dataclass(frozen=True, slots=True)
class ReconcileRow:
    source: str
    ledger_amount: Decimal | None
    source_amount: Decimal | None
    diff: Decimal | None
    diff_pct: Decimal | None
    flagged: bool


def _diff_pct(ledger: Decimal, expected: Decimal) -> Decimal:
    """相对差：以两侧绝对值较大者为基数（两侧同号放大时也成立）。"""
    base = max(abs(ledger), abs(expected))
    if base == 0:
        return Decimal("0.0000")
    return (abs(ledger - expected) / base).quantize(Decimal("0.0001"))


def reconcile_sources(
    ledger_by_source: Mapping[str, Decimal],
    expected_by_source: Mapping[str, Decimal],
    threshold_pct: Decimal = DEFAULT_THRESHOLD_PCT,
) -> list[ReconcileRow]:
    """对每个来源比较两侧合计，返回带标红位的明细行。

    - 双侧都有：差异率 > 阈值 → flagged；
    - 只有账本有：来源侧无登记（无主成本）→ flagged；
    - 只有来源有：漏记账 → flagged。
    """
    rows: list[ReconcileRow] = []
    for source in sorted(set(ledger_by_source) | set(expected_by_source)):
        ledger = ledger_by_source.get(source)
        expected = expected_by_source.get(source)
        if ledger is not None and expected is not None:
            pct = _diff_pct(ledger, expected)
            rows.append(
                ReconcileRow(
                    source=source,
                    ledger_amount=ledger,
                    source_amount=expected,
                    diff=(ledger - expected).quantize(Decimal("0.0001")),
                    diff_pct=pct,
                    flagged=pct > threshold_pct,
                )
            )
        elif ledger is not None:
            rows.append(
                ReconcileRow(
                    source=source,
                    ledger_amount=ledger,
                    source_amount=None,
                    diff=None,
                    diff_pct=None,
                    flagged=True,  # 来源侧没有这笔账
                )
            )
        else:
            assert expected is not None
            rows.append(
                ReconcileRow(
                    source=source,
                    ledger_amount=None,
                    source_amount=expected,
                    diff=None,
                    diff_pct=None,
                    flagged=True,  # 账本漏记
                )
            )
    return rows


def is_consistent(rows: list[ReconcileRow]) -> bool:
    return all(not r.flagged for r in rows)
