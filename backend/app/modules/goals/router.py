"""goals HTTP 路由。

依赖注入与 intake 同构：
- get_goals_repository 用 get_db 构造 Sql 实现，测试用 fake 注入；
- 模板端点是纯函数，无 DB 依赖。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.db import get_db
from app.core.deps import OwnerDep
from app.modules.goals import service as svc
from app.modules.goals.repository import GoalRepository, SqlGoalRepository
from app.modules.goals.schemas import (
    GoalCreate,
    GoalOut,
    GoalPatch,
    StopLossResult,
)
from app.modules.goals.templates import list_templates

MODULE = "goals"
OWNER_WINDOW = "W02"

router = APIRouter(prefix="/goals", tags=["goals"])


@router.get("/_status", summary="模块状态（骨架冒烟测试依赖；保留到统一清理波次）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "implemented"}


async def get_goals_repository(
    session=Depends(get_db),
) -> GoalRepository:
    return SqlGoalRepository(session)


GoalRepoDep = Annotated[GoalRepository, Depends(get_goals_repository)]


# ---------------------------------------------------------------------------
# W02-I03 · 目标卡
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=GoalOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建目标（双轨）",
)
async def create_goal(
    payload: GoalCreate, owner: OwnerDep, repo: GoalRepoDep
) -> GoalOut:
    return await svc.create_goal(repo, owner.subject, payload)


@router.get("/", response_model=list[GoalOut], summary="列出目标")
async def list_goals(
    owner: OwnerDep,
    repo: GoalRepoDep,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[GoalOut]:
    statuses = (status_filter,) if status_filter else None
    return await svc.list_goals(repo, owner.subject, statuses=statuses, limit=limit)


@router.get("/summary", summary="目标看板聚合接口（W02-I06）")
async def goals_summary(owner: OwnerDep, repo: GoalRepoDep) -> dict:
    """单请求聚合，禁止前端 N+1。预算数据缺失返回 unknown。"""
    summary = await svc.build_summary(repo, owner.subject)
    return summary.model_dump()


@router.get("/templates", summary="列出可用模板（W02-I04）")
async def list_templates_endpoint() -> list[dict]:
    return list_templates()


@router.get("/{goal_id}", response_model=GoalOut, summary="获取单条目标")
async def get_goal(owner: OwnerDep, repo: GoalRepoDep, goal_id: str) -> GoalOut:
    return await svc.get_goal(repo, owner.subject, goal_id)


@router.patch("/{goal_id}", response_model=GoalOut, summary="补丁目标（含状态机迁移）")
async def patch_goal(
    owner: OwnerDep,
    repo: GoalRepoDep,
    goal_id: str,
    payload: GoalPatch,
) -> GoalOut:
    return await svc.patch_goal(repo, owner.subject, goal_id, payload)


@router.post(
    "/{goal_id}/check-stop-loss",
    response_model=StopLossResult,
    summary="止损判定（W02-I04）",
)
async def check_stop_loss_endpoint(
    owner: OwnerDep, repo: GoalRepoDep, goal_id: str
) -> StopLossResult:
    return await svc.check_stop_loss(repo, owner.subject, goal_id)


@router.post(
    "/{goal_id}/degrade",
    response_model=GoalOut,
    summary="主动降级为兴趣（W02-I05，仅 active → degraded）",
)
async def degrade_endpoint(
    owner: OwnerDep, repo: GoalRepoDep, goal_id: str
) -> GoalOut:
    return await svc.degrade(repo, owner.subject, goal_id)


__all__ = ["router"]