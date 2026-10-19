"""双币种账本模块 —— owner: W07

钱六分项 + 注意力（min/outcome）、CPO 口径、订阅摊销、成本归集到任务/目标。

分层约定（不可越界）：
- `router.py`      只做 HTTP：解析入参、调用 service、格式化响应，**不写业务逻辑**
- `service.py`     业务规则与编排，**不依赖 Request/Response**，多步写入走事务
- `repository.py`  数据访问，只接受/返回领域对象或行记录
- `models.py`      ORM 模型，继承 `app.core.db.Base`

骨架自检端点（`/_status`、`/_ping`）按骨架约定保留且契约不变：
- `/_status` 的响应被 `backend/tests/test_smoke.py::test_module_status_is_stub` 钉死，
  改它需要 W01 同步更新该测试（不归本窗口），因此保持原样并在 handoff 中登记；
- 两者保留保证 OpenAPI 快照只增不改（本窗口新增端点均为纯新增操作）。
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import OwnerDep
from app.core.errors import NotFoundError, NotImplementedYetError
from app.modules.ledger import service
from app.modules.ledger.attention import month_bounds
from app.modules.ledger.repository import LedgerRepository
from app.modules.ledger.schemas import (
    PERIOD_PATTERN,
    AttentionStatsOut,
    BudgetStatusOut,
    LedgerEntryCreate,
    LedgerEntryListOut,
    LedgerEntryOut,
    LedgerSummaryOut,
    ReconcileReportOut,
    SubscriptionCreate,
    SubscriptionListOut,
    SubscriptionOut,
    SubscriptionUpdate,
)

MODULE = "ledger"
OWNER_WINDOW = "W07"

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("/_status", summary="模块占位状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "stub"}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    raise NotImplementedYetError(f"模块 {MODULE} 尚未实现，owner={OWNER_WINDOW}")


DBDep = Annotated[AsyncSession, Depends(get_db)]


async def get_ledger_repo(session: DBDep) -> LedgerRepository:
    return LedgerRepository(session)


LedgerRepoDep = Annotated[LedgerRepository, Depends(get_ledger_repo)]

PeriodDep = Annotated[str | None, Query(pattern=PERIOD_PATTERN)]


# ---------------------------------------------------------------- I01 成本归集


@router.post(
    "/entries",
    status_code=201,
    response_model=LedgerEntryOut,
    summary="成本归集（幂等：同 source+source_id 重复上报不重复记账）",
)
async def create_ledger_entry(
    payload: LedgerEntryCreate,
    response: Response,
    repo: LedgerRepoDep,
    _: OwnerDep,
) -> LedgerEntryOut:
    entry, created = await service.record_entry(repo, payload)
    response.status_code = 201 if created else 200
    return entry


@router.get("/entries", response_model=LedgerEntryListOut, summary="账本明细查询")
async def list_ledger_entries(
    repo: LedgerRepoDep,
    _: OwnerDep,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    category: Literal["subscription", "tools", "device", "rework", "qc", "labor"] | None = None,
    period: PeriodDep = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LedgerEntryListOut:
    start = end = None
    if period is not None:
        start, end = month_bounds(period)
    entries = await service.list_entries(
        repo, task_id=task_id, goal_id=goal_id, category=category, start=start, end=end, limit=limit
    )
    items = [LedgerEntryOut.model_validate(e) for e in entries]
    return LedgerEntryListOut(items=items, count=len(items))


@router.get(
    "/summary",
    response_model=LedgerSummaryOut,
    summary="周期六分项 + 注意力指标（缺失返回 null）",
)
async def ledger_summary(
    repo: LedgerRepoDep, _: OwnerDep, period: PeriodDep = None
) -> LedgerSummaryOut:
    return await service.summary(repo, period)


# ---------------------------------------------------------------- I02 注意力


@router.get(
    "/attention",
    response_model=AttentionStatsOut,
    summary="注意力账本：期内分钟 / 达标产出 / min-outcome",
)
async def ledger_attention(
    repo: LedgerRepoDep, _: OwnerDep, period: PeriodDep = None
) -> AttentionStatsOut:
    return await service.attention_stats(repo, period)


# ----------------------------------------------------- 只读契约（W02/W03 用）


@router.get(
    "/budget-status",
    response_model=BudgetStatusOut,
    summary="预算余量与是否冻结（W02-I06 / W03-I04 只读契约）",
)
async def ledger_budget_status(
    repo: LedgerRepoDep, _: OwnerDep, goal_id: uuid.UUID = Query()
) -> BudgetStatusOut:
    return await service.budget_status(repo, goal_id)


# ---------------------------------------------------------------- I05 订阅


@router.post(
    "/subscriptions",
    status_code=201,
    response_model=SubscriptionOut,
    summary="登记订阅（摊销 + 配额消耗率）",
)
async def create_subscription(
    payload: SubscriptionCreate, repo: LedgerRepoDep, _: OwnerDep
) -> SubscriptionOut:
    sub = await service.create_subscription(repo, payload)
    return service.subscription_out(sub, service.current_period_str())


@router.get(
    "/subscriptions",
    response_model=SubscriptionListOut,
    summary="订阅列表（含当期摊销、配额消耗率、告警位）",
)
async def list_subscriptions(
    repo: LedgerRepoDep, _: OwnerDep, period: PeriodDep = None
) -> SubscriptionListOut:
    items = await service.list_subscriptions_with_metrics(repo, period)
    return SubscriptionListOut(items=items, count=len(items))


@router.patch(
    "/subscriptions/{sub_id}",
    response_model=SubscriptionOut,
    summary="更新订阅（配额用量 / 启停）",
)
async def update_subscription(
    sub_id: uuid.UUID,
    payload: SubscriptionUpdate,
    repo: LedgerRepoDep,
    _: OwnerDep,
) -> SubscriptionOut:
    sub = await service.update_subscription(repo, sub_id, payload)
    if sub is None:
        raise NotFoundError("订阅不存在")
    return service.subscription_out(sub, service.current_period_str())


# ---------------------------------------------------------------- I06 对账导出


@router.get(
    "/reconcile",
    response_model=ReconcileReportOut,
    summary="对账：账本合计 vs 来源合计，差异 > 1% 标红",
)
async def reconcile_ledger(
    repo: LedgerRepoDep, _: OwnerDep, period: PeriodDep = None, dry_run: bool = True
) -> ReconcileReportOut:
    return await service.reconcile(repo, period, dry_run=dry_run)


@router.get("/export.csv", summary="导出当期账本明细 CSV（带 BOM）")
async def export_ledger_csv(
    repo: LedgerRepoDep, _: OwnerDep, period: PeriodDep = None
) -> Response:
    period, rows = await service.export_rows(repo, period)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM：Excel 打开中文不乱码
    writer = csv.writer(buf)
    writer.writerow(service.EXPORT_COLUMNS)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ledger-{period}.csv"'},
    )
