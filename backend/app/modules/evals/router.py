"""evals HTTP 路由（owner: W08）。

只做 HTTP：解析入参、调 service、格式化响应。业务规则在 service.py，
执行在 runner.py，数据访问在 repository.py。

异步约定（验收 ②）：`POST /evals/runs` 只落库并把执行丢给 BackgroundTasks，
立刻返回 202 + run_id；请求线程里绝不调用模型。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status

from app.core.db import get_db
from app.core.deps import OwnerDep, require_scope
from app.modules.evals import service as svc
from app.modules.evals.repository import EvalRepository, SqlEvalRepository, to_task_out
from app.modules.evals.runner import execute_run, provider_for
from app.modules.evals.schemas import (
    Dimension,
    RadarOut,
    RunCreate,
    RunDetail,
    RunOut,
    SeedResult,
    TaskCreate,
    TaskList,
    TaskOut,
    TaskUpdate,
)

MODULE = "evals"
OWNER_WINDOW = "W08"

logger = logging.getLogger("afc.evals.router")

router = APIRouter(prefix="/evals", tags=["evals"])

ReadDep = Annotated[object, Depends(require_scope("evals:read"))]
WriteDep = Annotated[object, Depends(require_scope("evals:write"))]


async def get_evals_repository(session=Depends(get_db)) -> EvalRepository:
    """生产实现；测试用 dependency_overrides 注入内存 fake。"""
    return SqlEvalRepository(session)


EvalsRepoDep = Annotated[EvalRepository, Depends(get_evals_repository)]


@router.get("/_status", summary="模块状态（骨架冒烟测试依赖；保留到统一清理波次）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "implemented"}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    """保留给契约快照：删掉已上线端点会被 CI 判为 breaking change（scripts/openapi_snapshot.py）。"""
    return {"module": MODULE, "pong": "1"}


# ---------------------------------------------------------------------------
# 题库 CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增题目",
)
async def create_task(
    payload: TaskCreate,
    _: WriteDep,
    repo: EvalsRepoDep,
) -> TaskOut:
    return await svc.create_task(repo, payload)


@router.post(
    "/tasks/seed",
    response_model=SeedResult,
    summary="导入 20 道种子题（幂等）",
)
async def seed_tasks(_: WriteDep, repo: EvalsRepoDep) -> SeedResult:
    return await svc.seed_question_bank(repo)


@router.get("/tasks", response_model=TaskList, summary="题目列表")
async def list_tasks(
    _: ReadDep,
    repo: EvalsRepoDep,
    dimension: Annotated[Dimension | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TaskList:
    return await svc.list_tasks(
        repo, dimension=dimension, include_archived=include_archived, limit=limit
    )


@router.get("/tasks/{task_id}", response_model=TaskOut, summary="题目详情")
async def get_task(task_id: str, _: ReadDep, repo: EvalsRepoDep) -> TaskOut:
    return to_task_out(await repo.get_task(task_id))


@router.patch("/tasks/{task_id}", response_model=TaskOut, summary="修改题目")
async def patch_task(
    task_id: str, payload: TaskUpdate, _: WriteDep, repo: EvalsRepoDep
) -> TaskOut:
    return await svc.update_task(repo, task_id, payload)


@router.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="归档题目（软删，历史 run 仍可追溯）",
)
async def delete_task(task_id: str, _: WriteDep, repo: EvalsRepoDep) -> Response:
    await repo.archive_task(task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


async def _run_in_background(run_id: str, provider_name: str) -> None:
    """后台执行入口：自己开 session，请求的 session 已经关闭了。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        repo = SqlEvalRepository(session)
        try:
            run = await repo.get_run(run_id)
            tasks = await svc.load_run_tasks(repo, run)
            await execute_run(repo, run, tasks, provider_for(provider_name))
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常吞成静默失败
            logger.exception("eval run failed", extra={"run_id": run_id})
            try:
                await repo.update_run(
                    run_id,
                    {
                        "status": "failed",
                        "error_code": "runner_error",
                        "error_message": f"{type(exc).__name__}: {exc}"[:500],
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to mark run as failed", extra={"run_id": run_id})


@router.post(
    "/runs",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="发起一次评测（异步，立即返回 run_id）",
)
async def create_run(
    payload: RunCreate,
    owner: OwnerDep,
    _: WriteDep,
    repo: EvalsRepoDep,
    background: BackgroundTasks,
    response: Response,
) -> RunOut:
    run = await svc.create_run(repo, owner=owner.subject, payload=payload)
    background.add_task(_run_in_background, run.id, run.provider)
    response.headers["Location"] = f"/api/v1/evals/runs/{run.id}"
    return RunOut.model_validate(
        {
            "id": run.id,
            "status": run.status,
            "provider": run.provider,
            "models": list(run.models or []),
            "task_count": run.task_count,
            "result_count": run.result_count,
            "cache_hits": run.cache_hits,
            "cost_usd": float(run.cost_usd or 0),
            "cost_cap_cny": float(run.cost_cap_cny),
            "error_code": run.error_code,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
    )


@router.get("/runs/{run_id}", response_model=RunDetail, summary="运行详情与结果")
async def get_run(run_id: str, _: ReadDep, repo: EvalsRepoDep) -> RunDetail:
    return await svc.get_run_detail(repo, run_id)


# ---------------------------------------------------------------------------
# 雷达聚合（W08-I02）
# ---------------------------------------------------------------------------


@router.get(
    "/radar",
    response_model=RadarOut,
    summary="8 维雷达（缺样本的维度返回 insufficient）",
)
async def get_radar(
    _: ReadDep,
    repo: EvalsRepoDep,
    week: Annotated[
        str | None,
        Query(
            description="ISO 周（2026-W42）或该周内任意日期（2026-10-14）；留空为本周",
            max_length=10,
        ),
    ] = None,
    models: Annotated[
        list[str] | None,
        Query(description="只关心这几个模型；留空为当周出现过的全部模型"),
    ] = None,
) -> RadarOut:
    return await svc.get_radar(repo, week=week, models=models)
