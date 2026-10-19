"""evals 业务服务层。

分层：router 只做 HTTP，runner 只做执行，**业务规则都在这里**。
service 不依赖 Request/Response，也不依赖 ORM Session——只依赖 `EvalRepository`。
"""

from __future__ import annotations

from datetime import datetime

from app.core.errors import ConflictError, ValidationError

from .ceiling import build_ceiling, check_capability
from .models import EvalRun, EvalTask
from .radar import aggregate_radar, parse_week
from .repository import EvalRepository, new_id, to_task_out
from .runner import default_cost_cap_cny
from .schemas import (
    DIMENSIONS,
    CeilingCheckOut,
    CeilingCheckRequest,
    CeilingOut,
    RadarOut,
    ResultOut,
    RunCreate,
    RunDetail,
    RunOut,
    SeedResult,
    TaskCreate,
    TaskList,
    TaskOut,
    TaskUpdate,
)
from .seed import SEED_TASKS

# 题库验收线：总数 20、8 个维度各 ≥2 题
REQUIRED_TOTAL = 20
REQUIRED_PER_DIMENSION = 2


def validate_question_bank(counts: dict[str, int]) -> tuple[bool, list[str]]:
    """检查题库是否满足"20 题、8 维各 ≥2 题"。返回 (ok, problems)。"""
    problems: list[str] = []
    total = sum(counts.values())
    if total < REQUIRED_TOTAL:
        problems.append(f"题目总数 {total} < {REQUIRED_TOTAL}")
    missing = [d for d in DIMENSIONS if counts.get(d, 0) == 0]
    if missing:
        problems.append(f"缺少维度: {', '.join(missing)}")
    thin = [f"{d}={counts.get(d, 0)}" for d in DIMENSIONS if 0 < counts.get(d, 0) < REQUIRED_PER_DIMENSION]
    if thin:
        problems.append(f"维度题量不足 {REQUIRED_PER_DIMENSION}: {', '.join(thin)}")
    return (not problems, problems)


async def seed_question_bank(repo: EvalRepository, *, now: datetime | None = None) -> SeedResult:
    """导入 20 道种子题。按 (dimension, title) 去重，可重复调用。"""
    stamp = now or datetime.utcnow()
    existing = await repo.list_tasks(include_archived=True, limit=1000)
    seen = {(t.dimension, t.title) for t in existing}

    created = 0
    for item in SEED_TASKS:
        if (item.dimension, item.title) in seen:
            continue
        await repo.create_task(
            EvalTask(
                id=new_id(),
                dimension=item.dimension,
                title=item.title,
                prompt=item.prompt,
                rubric=list(item.rubric),
                max_score=100,
                weight=item.weight,
                active=True,
                archived_at=None,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        seen.add((item.dimension, item.title))
        created += 1

    tasks = await repo.list_tasks(include_archived=True, limit=1000)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.dimension] = counts.get(t.dimension, 0) + 1
    ok, problems = validate_question_bank(counts)
    return SeedResult(
        created=created,
        skipped=len(SEED_TASKS) - created,
        total=len(tasks),
        per_dimension=counts,
        valid=ok,
        problems=problems,
    )


async def create_task(
    repo: EvalRepository, payload: TaskCreate, *, now: datetime | None = None
) -> TaskOut:
    stamp = now or datetime.utcnow()
    task = EvalTask(
        id=new_id(),
        dimension=payload.dimension,
        title=payload.title,
        prompt=payload.prompt,
        rubric=[r.model_dump() for r in payload.rubric],
        max_score=payload.max_score,
        weight=payload.weight,
        active=True,
        archived_at=None,
        created_at=stamp,
        updated_at=stamp,
    )
    return to_task_out(await repo.create_task(task))


async def update_task(
    repo: EvalRepository, task_id: str, payload: TaskUpdate, *, now: datetime | None = None
) -> TaskOut:
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not fields:
        raise ValidationError("没有需要更新的字段")
    if "rubric" in fields:
        fields["rubric"] = [
            r.model_dump() if hasattr(r, "model_dump") else r for r in payload.rubric or []
        ]
    fields["updated_at"] = now or datetime.utcnow()
    return to_task_out(await repo.update_task(task_id, fields))


async def list_tasks(
    repo: EvalRepository,
    *,
    dimension: str | None = None,
    include_archived: bool = False,
    limit: int = 200,
) -> TaskList:
    tasks = await repo.list_tasks(
        dimension=dimension, include_archived=include_archived, limit=limit
    )
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.dimension] = counts.get(t.dimension, 0) + 1
    return TaskList(items=[to_task_out(t) for t in tasks], total=len(tasks), per_dimension=counts)


async def create_run(
    repo: EvalRepository,
    *,
    owner: str,
    payload: RunCreate,
    now: datetime | None = None,
) -> EvalRun:
    """只创建 run 记录并返回，**不执行**——执行由 router 丢给后台任务。"""
    stamp = now or datetime.utcnow()
    models: list[str] = []
    for m in payload.models:
        name = m.strip()
        if not name:
            raise ValidationError("models 不能包含空字符串")
        if name not in models:
            models.append(name)
    if not models:
        raise ValidationError("至少需要一个模型")

    if payload.task_ids:
        tasks: list[EvalTask] = []
        for tid in dict.fromkeys(payload.task_ids):
            tasks.append(await repo.get_task(tid))
    else:
        tasks = await repo.list_tasks(include_archived=False, limit=1000)
    if not tasks:
        raise ValidationError("题库为空：请先导入题目（POST /evals/tasks/seed）")

    cap = float(payload.cost_cap_cny) if payload.cost_cap_cny else default_cost_cap_cny()
    run = EvalRun(
        id=new_id(),
        status="queued",
        provider=payload.provider,
        models=models,
        task_ids=[t.id for t in tasks],
        cost_cap_cny=cap,
        cost_usd=0,
        task_count=len(tasks) * len(models),
        result_count=0,
        cache_hits=0,
        created_by=owner,
        created_at=stamp,
    )
    return await repo.create_run(run)


async def load_run_tasks(repo: EvalRepository, run: EvalRun) -> list[EvalTask]:
    """后台执行时按 run.task_ids 重新取题（题目可能已被改，按最新版本跑）。"""
    tasks: list[EvalTask] = []
    for tid in run.task_ids:
        try:
            tasks.append(await repo.get_task(tid, include_archived=True))
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(f"题目 {tid} 已不存在，无法执行该 run") from exc
    return tasks


async def get_run_detail(repo: EvalRepository, run_id: str) -> RunDetail:
    run = await repo.get_run(run_id)
    rows = await repo.list_results(run_id)
    return RunDetail(
        run=RunOut.model_validate(
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
        ),
        results=[
            ResultOut(
                task_id=r.task_id,
                model=r.model,
                score=float(r.score),
                cost_usd=float(r.cost_usd),
                latency_ms=r.latency_ms,
                raw_ref=r.raw_ref,
                cached=r.cached,
            )
            for r in rows
        ],
    )


async def get_radar(
    repo: EvalRepository,
    *,
    week: str | None = None,
    models: list[str] | None = None,
    now: datetime | None = None,
) -> RadarOut:
    """一周的 8 维雷达。聚合本身是纯函数（radar.aggregate_radar），这里只做取数。"""
    stamp = now or datetime.utcnow()
    window = parse_week(week, now=stamp)
    samples = await repo.list_result_samples(start=window.start, end=window.end)
    payload = aggregate_radar(samples, window=window, models=models)
    return RadarOut.model_validate(payload)


async def get_ceiling(
    repo: EvalRepository,
    *,
    week: str | None = None,
    models: list[str] | None = None,
    now: datetime | None = None,
) -> CeilingOut:
    """一周的能力上限。与雷达同源同窗口，避免两处数字对不上。"""
    stamp = now or datetime.utcnow()
    window = parse_week(week, now=stamp)
    samples = await repo.list_result_samples(start=window.start, end=window.end)
    payload = build_ceiling(samples, window=window, models=models)
    return CeilingOut.model_validate(payload)


async def check_project(
    repo: EvalRepository,
    payload: CeilingCheckRequest,
    *,
    now: datetime | None = None,
) -> CeilingCheckOut:
    """立项校验：`GET /evals/ceiling` 的判定版，只回答"我这一步能不能做"。"""
    ceiling = await get_ceiling(repo, week=payload.week, models=payload.models, now=now)
    result = check_capability(
        ceiling.model_dump(), requested=list(payload.dimensions or [])
    )
    return CeilingCheckOut.model_validate(result)


__all__ = [
    "REQUIRED_PER_DIMENSION",
    "REQUIRED_TOTAL",
    "check_project",
    "create_run",
    "create_task",
    "get_ceiling",
    "get_radar",
    "get_run_detail",
    "list_tasks",
    "load_run_tasks",
    "seed_question_bank",
    "update_task",
    "validate_question_bank",
]
