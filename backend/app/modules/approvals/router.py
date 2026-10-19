"""闸门（L0-L3 审批）+ 带宽熔断 —— owner: W07

L0 战略（必须人）/ L1 例外（抽样）/ L2 常规（机器默认）/ L3 机械（不可见）。
闸门什么都问人是反模式：L0 ≤2/日、L1 ≤8/日，超了自动转明日。

骨架自检端点（`/_status`、`/_ping`）按骨架约定保留且契约不变
（原因见 ledger/router.py 模块注释）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import OwnerDep
from app.core.errors import NotImplementedYetError, ValidationError
from app.modules.approvals import service
from app.modules.approvals.repository import ApprovalRepository
from app.modules.approvals.schemas import (
    ApprovalCreate,
    ApprovalCreateOut,
    ApprovalOut,
    ApprovalQueueOut,
    BandwidthOut,
    DecideIn,
    DecideOut,
)
from app.modules.ledger.repository import LedgerRepository
from app.modules.ledger.router import get_ledger_repo

MODULE = "approvals"
OWNER_WINDOW = "W07"

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/_status", summary="模块占位状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "stub"}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    raise NotImplementedYetError(f"模块 {MODULE} 尚未实现，owner={OWNER_WINDOW}")


DBDep = Annotated[AsyncSession, Depends(get_db)]


async def get_approval_repo(session: DBDep) -> ApprovalRepository:
    return ApprovalRepository(session)


ApprovalRepoDep = Annotated[ApprovalRepository, Depends(get_approval_repo)]
# 注意力账本归 W07 的 ledger 模块：夜间静默判定与决策耗时入账都要它
LedgerRepoDep = Annotated[LedgerRepository, Depends(get_ledger_repo)]


# ---------------------------------------------------------------- I03 创建与队列


@router.post(
    "",
    status_code=201,
    response_model=ApprovalCreateOut,
    summary="创建审批（L0/L1 超日限额自动转明日）",
)
async def create_approval(
    payload: ApprovalCreate, repo: ApprovalRepoDep, _: OwnerDep
) -> ApprovalCreateOut:
    approval, deferred = await service.create_approval(repo, payload)
    return ApprovalCreateOut(approval=ApprovalOut.model_validate(approval), deferred=deferred)


@router.get(
    "/queue",
    response_model=ApprovalQueueOut,
    summary="待决策队列（默认 L0/L1；抽检页传 levels=L2,L3）",
)
async def list_approval_queue(
    repo: ApprovalRepoDep,
    _: OwnerDep,
    levels: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApprovalQueueOut:
    parsed: tuple[str, ...] | None = None
    if levels is not None:
        wanted = [lv.strip().upper() for lv in levels.split(",") if lv.strip()]
        invalid = [lv for lv in wanted if lv not in ("L0", "L1", "L2", "L3")]
        if invalid:
            raise ValidationError(f"非法 level: {invalid}")
        parsed = tuple(wanted)
    items = await service.queue(repo, levels=parsed, limit=limit)
    outs = [ApprovalOut.model_validate(a) for a in items]
    return ApprovalQueueOut(items=outs, count=len(outs))


# ---------------------------------------------------------------- I03 决策


@router.post(
    "/{approval_id}/decide",
    response_model=DecideOut,
    summary="决策（夜间 L0 只记录待明日确认，返回 202）",
)
async def decide_approval(
    approval_id: uuid.UUID,
    payload: DecideIn,
    response: Response,
    approval_repo: ApprovalRepoDep,
    ledger_repo: LedgerRepoDep,
    principal: OwnerDep,
) -> DecideOut:
    result = await service.decide(
        approval_repo,
        approval_id,
        payload,
        decider=principal.subject,
        ledger_repo=ledger_repo,
    )
    response.status_code = 202 if result.held else 200
    return DecideOut(approval=ApprovalOut.model_validate(result.approval), held=result.held)


# ---------------------------------------------------------------- I04 带宽熔断


@router.get(
    "/bandwidth",
    response_model=BandwidthOut,
    summary="带宽熔断快照（纯函数 evaluate 的输入与判定）",
)
async def approval_bandwidth(
    approval_repo: ApprovalRepoDep,
    ledger_repo: LedgerRepoDep,
    _: OwnerDep,
) -> BandwidthOut:
    config, inp, verdict = await service.bandwidth_status(approval_repo, ledger_repo)
    return BandwidthOut(
        config={
            "daily_decision_limit": config.daily_decision_limit,
            "duration_window_days": config.duration_window_days,
            "duration_rise_pct": config.duration_rise_pct,
            "dispatch_cut_pct": config.dispatch_cut_pct,
            "veto_floor_pct": config.veto_floor_pct,
            "veto_ceiling_pct": config.veto_ceiling_pct,
            "review_sample": config.review_sample,
            "night_start_hour": config.night_start_hour,
            "night_end_hour": config.night_end_hour,
            "night_tz_offset_hours": config.night_tz_offset_hours,
        },
        input={
            "decisions_today": inp.decisions_today,
            "daily_avg_minutes": list(inp.daily_avg_minutes),
            "veto_rate_recent": inp.veto_rate_recent,
            "veto_rate_baseline": inp.veto_rate_baseline,
            "now_hour": inp.now_hour,
        },
        verdict={
            "defer_non_critical": verdict.defer_non_critical,
            "dispatch_factor": verdict.dispatch_factor,
            "fatigue_suspected": verdict.fatigue_suspected,
            "review_sample": verdict.review_sample,
            "night_hold_l0": verdict.night_hold_l0,
            "reasons": list(verdict.reasons),
        },
    )
