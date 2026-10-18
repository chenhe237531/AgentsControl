"""账本业务规则与编排。不依赖 Request/Response，多步写入走事务。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.modules.ledger import amortize
from app.modules.ledger import reconcile as reconcile_mod
from app.modules.ledger.attention import compute_min_per_outcome, current_period, month_bounds
from app.modules.ledger.models import (
    CATEGORIES,
    AttentionEntry,
    LedgerEntry,
    Subscription,
)
from app.modules.ledger.repository import GATE_SOURCE, LedgerRepository
from app.modules.ledger.schemas import (
    AttentionStatsOut,
    BudgetStatusOut,
    LedgerEntryCreate,
    LedgerSummaryOut,
    ReconcileReportOut,
    ReconcileRowOut,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)

ZERO = Decimal("0.0000")
QUOTA_THRESHOLD = amortize.QUOTA_ALERT_THRESHOLD


def utcnow() -> datetime:
    return datetime.now(UTC)


def current_period_str() -> str:
    return current_period(utcnow().date())


# ---------------------------------------------------------------- 钱账本（I01）


async def record_entry(
    repo: LedgerRepository, payload: LedgerEntryCreate, *, now: datetime | None = None
) -> tuple[LedgerEntry, bool]:
    """成本归集。幂等：同一 (source, source_id) 重复上报不重复记账。

    返回 (entry, created)。created=False 表示命中已有记录（回放）。
    """
    now = now or utcnow()
    existing = await repo.find_entry(payload.source, payload.source_id)
    if existing is not None:
        return existing, False
    entry = LedgerEntry(
        occurred_at=payload.occurred_at or now,
        category=payload.category,
        amount=payload.amount,
        currency=payload.currency,
        task_id=payload.task_id,
        goal_id=payload.goal_id,
        memo=payload.memo,
        source=payload.source,
        source_id=payload.source_id,
    )
    try:
        await repo.add_entry(entry)
        await repo.commit()
    except IntegrityError:
        # 并发回放：UNIQUE(source, source_id) 兜底，重读返回已有条目
        existing = await repo.find_entry(payload.source, payload.source_id)
        if existing is None:
            raise
        return existing, False
    return entry, True


async def list_entries(
    repo: LedgerRepository,
    *,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    category: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
) -> list[LedgerEntry]:
    return await repo.list_entries(
        task_id=task_id,
        goal_id=goal_id,
        category=category,
        start=start,
        end=end,
        limit=limit,
    )


async def summary(
    repo: LedgerRepository, period: str | None = None
) -> LedgerSummaryOut:
    """周期内六分项 + 注意力指标。六分项缺失记 0，注意力缺失记 null。"""
    period = period or current_period(utcnow().date())
    start, end = month_bounds(period)
    cats = await repo.sum_by_category(start, end)
    categories = {c: cats.get(c, ZERO) for c in CATEGORIES}
    total = sum(categories.values(), ZERO)
    attention = await attention_stats(repo, period)
    return LedgerSummaryOut(
        period=period,
        currency="CNY",
        categories=categories,
        total=total,
        attention=attention,
    )


# ------------------------------------------------------------ 注意力（I02）


async def attention_stats(
    repo: LedgerRepository, period: str | None = None
) -> AttentionStatsOut:
    period = period or current_period(utcnow().date())
    start, end = month_bounds(period)
    minutes = await repo.sum_attention_minutes(start, end)
    outcomes = await repo.count_outcomes(start, end)
    return AttentionStatsOut(
        period=period,
        minutes=minutes,
        outcomes=outcomes,
        min_per_outcome=compute_min_per_outcome(minutes, outcomes),
    )


async def record_gate_minutes(
    repo: LedgerRepository,
    *,
    source_id: str,
    minutes: Decimal,
    now: datetime | None = None,
    memo: str | None = None,
) -> AttentionEntry:
    """闸门操作耗时入账（approvals 模块在 decide 时调用）。"""
    now = now or utcnow()
    entry = AttentionEntry(
        occurred_at=now,
        minutes=minutes,
        source=GATE_SOURCE,
        source_id=source_id,
        memo=memo,
    )
    await repo.add_attention(entry)
    await repo.commit()
    return entry


# ------------------------------------------------------------ 预算状态（契约）


async def budget_status(
    repo: LedgerRepository, goal_id: uuid.UUID
) -> BudgetStatusOut:
    """预算余量与是否冻结（W02-I06 / W03-I04 的只读契约）。

    预算未知（W02 未落地）→ budget/remaining = null，frozen=False。
    花超预算 → frozen=True（预算阀门据此停派）。
    """
    spent = await repo.sum_for_goal(goal_id)
    budget = await repo.get_goal_budget(goal_id)
    remaining = budget - spent if budget is not None else None
    frozen = budget is not None and spent > budget
    return BudgetStatusOut(
        goal_id=goal_id, budget=budget, spent=spent, remaining=remaining, frozen=frozen
    )


# ------------------------------------------------------------ 订阅（I05）


async def create_subscription(
    repo: LedgerRepository, payload: SubscriptionCreate
) -> Subscription:
    sub = Subscription(
        name=payload.name,
        monthly_fee=payload.monthly_fee,
        quota_limit=payload.quota_limit,
        quota_used=0,
        amortize_mode=payload.amortize_mode,
        started_on=payload.started_on,
        active=True,
    )
    await repo.add_subscription(sub)
    await repo.commit()
    return sub


async def update_subscription(
    repo: LedgerRepository, sub_id: uuid.UUID, payload: SubscriptionUpdate
) -> Subscription | None:
    sub = await repo.get_subscription(sub_id)
    if sub is None:
        return None
    if payload.quota_used is not None:
        sub.quota_used = payload.quota_used
    if payload.active is not None:
        sub.active = payload.active
    await repo.commit()
    return sub


def _subscription_metrics(
    sub: Subscription, period: str
) -> tuple[Decimal | None, Decimal | None, bool]:
    """（当期摊销额, 配额消耗率, 是否告警）。"""
    start, end = month_bounds(period)
    if sub.amortize_mode == "usage":
        amortized = amortize.amortize_usage(
            sub.monthly_fee, sub.quota_used, sub.quota_limit
        )
    else:
        amortized = amortize.amortize_calendar(
            sub.monthly_fee, sub.started_on, start.date(), end.date()
        )
    consumption = amortize.quota_consumption(sub.quota_used, sub.quota_limit)
    return amortized, consumption, amortize.quota_alert(consumption, QUOTA_THRESHOLD)


def subscription_out(sub: Subscription, period: str) -> SubscriptionOut:
    amortized, consumption, alert = _subscription_metrics(sub, period)
    out = SubscriptionOut.model_validate(sub)
    out.amortized_current_period = amortized
    out.quota_consumption = consumption
    out.quota_alert = alert
    return out


async def list_subscriptions_with_metrics(
    repo: LedgerRepository, period: str | None = None
) -> list[SubscriptionOut]:
    period = period or current_period(utcnow().date())
    subs = await repo.list_subscriptions()
    return [subscription_out(s, period) for s in subs]


# ------------------------------------------------------------ 对账与导出（I06）


async def reconcile(
    repo: LedgerRepository,
    period: str | None = None,
    *,
    dry_run: bool = True,
    threshold_pct: Decimal = reconcile_mod.DEFAULT_THRESHOLD_PCT,
) -> ReconcileReportOut:
    """账本合计 vs 来源合计。差异 > 阈值标红并给差值明细。

    来源侧登记：订阅按摊销口径给出应有金额（calendar 模式）。
    dry_run=True 只出报告不落任何调整（本端点本身只读，语义上仍保留开关）。
    """
    period = period or current_period(utcnow().date())
    start, end = month_bounds(period)
    ledger_by_source = await repo.sum_by_source(start, end)
    expected: dict[str, Decimal] = {}
    for sub in await repo.list_subscriptions():
        if not sub.active:
            continue
        amount = amortize.amortize_calendar(
            sub.monthly_fee, sub.started_on, start.date(), end.date()
        )
        expected["subscription"] = expected.get("subscription", ZERO) + amount
    pure_rows = reconcile_mod.reconcile_sources(ledger_by_source, expected, threshold_pct)
    rows = [
        ReconcileRowOut(
            source=r.source,
            ledger_amount=r.ledger_amount,
            source_amount=r.source_amount,
            diff=r.diff,
            diff_pct=r.diff_pct,
            flagged=r.flagged,
        )
        for r in pure_rows
    ]
    return ReconcileReportOut(
        period=period,
        threshold_pct=threshold_pct,
        dry_run=dry_run,
        consistent=reconcile_mod.is_consistent(pure_rows),
        rows=rows,
    )


EXPORT_COLUMNS = (
    "occurred_at",
    "category",
    "amount",
    "currency",
    "task_id",
    "goal_id",
    "source",
    "source_id",
    "memo",
)


async def export_rows(
    repo: LedgerRepository, period: str | None = None, *, limit: int = 5000
) -> tuple[str, list[list[str | None]]]:
    """导出当期明细（CSV 由 router 渲染，BOM 在响应层加）。"""
    period = period or current_period(utcnow().date())
    start, end = month_bounds(period)
    entries = await repo.list_entries(start=start, end=end, limit=limit)
    entries.sort(key=lambda e: e.occurred_at)
    rows = [
        [
            e.occurred_at.isoformat(),
            e.category,
            f"{e.amount:.4f}",
            e.currency,
            str(e.task_id) if e.task_id else None,
            str(e.goal_id) if e.goal_id else None,
            e.source,
            e.source_id,
            e.memo,
        ]
        for e in entries
    ]
    return period, rows


def today() -> date:
    return utcnow().date()
