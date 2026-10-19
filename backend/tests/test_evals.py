"""W08 · evals 模块测试（W08-I01 题库与运行框架）。

原则（与 W02 一致）：
- 不依赖真实 PG / Redis，也不出网——fake repository + fake provider；
- 路由层用 TestClient 走完整 HTTP，断言 status_code 与结构化 error；
- 执行逻辑（成本上限 / 缓存 / provider 失败）直接对 runner 做单测。

验收映射：
① 题库 CRUD + 20 题 8 维各 ≥2 → test_seed_* / test_task_crud_*
② 运行异步、POST 立即返回 run_id → test_create_run_returns_queued_before_execution
③ 结果记录 model/cost_usd/latency_ms/score/raw_ref → test_execute_run_records_all_fields
④ fake provider 不出网 → 全部用 FakeProvider
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.errors import NotFoundError, ValidationError
from app.core.security import create_token
from app.main import create_app
from app.modules.evals.models import EvalResult, EvalRun, EvalTask
from app.modules.evals.radar import RadarSample, aggregate_radar, parse_week
from app.modules.evals.repository import cache_key
from app.modules.evals.runner import (
    FakeProvider,
    ProviderResult,
    execute_run,
    provider_for,
)
from app.modules.evals.schemas import DIMENSIONS
from app.modules.evals.seed import SEED_TASKS
from app.modules.evals.service import validate_question_bank

# 包 `__init__` 把 `router` 这个名字绑定成了 APIRouter，`from ... import router`
# 拿到的是对象而不是模块；显式取模块，才能 monkeypatch `_run_in_background`。
evals_router = importlib.import_module("app.modules.evals.router")

OWNER = "owner-1"
TOKEN = create_token(OWNER, "access", extra={"role": "owner"})
VIEWER = create_token(OWNER, "access", extra={"role": "viewer"})
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VIEWER_AUTH = {"Authorization": f"Bearer {VIEWER}"}

NOW = datetime(2026, 10, 18, 12, 0, 0)


# ---------------------------------------------------------------------------
# Fake repository：内存实现，覆盖 EvalRepository Protocol
# ---------------------------------------------------------------------------


class FakeEvalRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, EvalTask] = {}
        self.runs: dict[str, EvalRun] = {}
        self.results: list[EvalResult] = []

    async def create_task(self, task: EvalTask) -> EvalTask:
        self.tasks[task.id] = task
        return task

    async def get_task(self, task_id: str, *, include_archived: bool = False) -> EvalTask:
        task = self.tasks.get(task_id)
        if task is None or (not include_archived and task.archived_at is not None):
            raise NotFoundError("题目不存在或已归档")
        return task

    async def list_tasks(
        self,
        *,
        dimension: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[EvalTask]:
        rows = [
            t
            for t in self.tasks.values()
            if (include_archived or t.archived_at is None)
            and (dimension is None or t.dimension == dimension)
        ]
        rows.sort(key=lambda t: (t.dimension, t.created_at))
        return rows[:limit]

    async def count_tasks(self) -> int:
        return len([t for t in self.tasks.values() if t.archived_at is None])

    async def update_task(self, task_id: str, fields: dict) -> EvalTask:
        task = await self.get_task(task_id)
        for key, value in fields.items():
            setattr(task, key, value)
        return task

    async def archive_task(self, task_id: str) -> EvalTask:
        task = await self.get_task(task_id)
        task.active = False
        task.archived_at = datetime.utcnow()
        return task

    async def create_run(self, run: EvalRun) -> EvalRun:
        self.runs[run.id] = run
        return run

    async def get_run(self, run_id: str) -> EvalRun:
        run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError("评测运行不存在")
        return run

    async def update_run(self, run_id: str, fields: dict) -> EvalRun:
        run = await self.get_run(run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        return run

    async def add_results(self, results: list[EvalResult]) -> int:
        self.results.extend(results)
        return len(results)

    async def list_results(self, run_id: str) -> list[EvalResult]:
        return [r for r in self.results if r.run_id == run_id]

    async def find_cached(self, key: str, *, since: datetime) -> EvalResult | None:
        hits = [
            r
            for r in self.results
            if r.cache_key == key and not r.cached and r.created_at >= since
        ]
        if not hits:
            return None
        return max(hits, key=lambda r: r.created_at)

    async def list_result_samples(self, *, start: datetime, end: datetime) -> list[RadarSample]:
        """雷达取数：按结果时间过滤 + 用题目表补维度（等价于 SQL 的 join）。"""
        rows: list[RadarSample] = []
        for r in self.results:
            if not (start <= r.created_at < end):
                continue
            task = self.tasks.get(r.task_id)
            if task is None:
                continue
            rows.append(
                RadarSample(
                    dimension=task.dimension,
                    model=r.model,
                    score=float(r.score),
                    cost_usd=float(r.cost_usd),
                    latency_ms=int(r.latency_ms),
                    cached=bool(r.cached),
                )
            )
        return rows


@pytest.fixture()
def repo() -> FakeEvalRepo:
    return FakeEvalRepo()


@pytest.fixture()
def client(repo: FakeEvalRepo) -> TestClient:
    app = create_app()
    app.dependency_overrides[evals_router.get_evals_repository] = lambda: repo
    return TestClient(app)


async def _seed(repo: FakeEvalRepo) -> None:
    from app.modules.evals import service as svc
    from app.modules.evals.schemas import TaskCreate

    for item in SEED_TASKS:
        await svc.create_task(
            repo,
            TaskCreate(
                dimension=item.dimension,
                title=item.title,
                prompt=item.prompt,
                rubric=item.rubric,
                weight=item.weight,
            ),
            now=NOW,
        )


# ---------------------------------------------------------------------------
# ① 题库：20 题、8 维各 ≥2
# ---------------------------------------------------------------------------


def test_seed_creates_twenty_tasks_over_eight_dimensions(client: TestClient) -> None:
    r = client.post("/api/v1/evals/tasks/seed", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 20
    assert body["total"] == 20
    assert body["valid"] is True
    assert body["problems"] == []
    assert len(body["per_dimension"]) == len(DIMENSIONS) == 8
    for dimension in DIMENSIONS:
        assert body["per_dimension"][dimension] >= 2, dimension


def test_seed_is_idempotent(client: TestClient) -> None:
    first = client.post("/api/v1/evals/tasks/seed", headers=AUTH).json()
    second = client.post("/api/v1/evals/tasks/seed", headers=AUTH).json()
    assert first["created"] == 20
    assert second["created"] == 0
    assert second["skipped"] == 20
    assert second["total"] == 20


def test_validate_question_bank_flags_thin_and_missing() -> None:
    ok, problems = validate_question_bank({"long_form": 2, "code": 1})
    assert ok is False
    assert any("题目总数" in p for p in problems)
    assert any("缺少维度" in p for p in problems)
    assert any("题量不足" in p for p in problems)

    counts = dict.fromkeys(DIMENSIONS, 3)
    ok2, problems2 = validate_question_bank(counts)
    assert ok2 is True
    assert problems2 == []


# ---------------------------------------------------------------------------
# ① 题库 CRUD
# ---------------------------------------------------------------------------


def test_task_crud_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/v1/evals/tasks",
        headers=AUTH,
        json={
            "dimension": "code",
            "title": "临时题",
            "prompt": "写一个二分查找",
            "rubric": [{"criterion": "正确", "weight": 1}],
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["dimension"] == "code"
    assert task["active"] is True
    task_id = task["id"]

    listed = client.get("/api/v1/evals/tasks", headers=AUTH, params={"dimension": "code"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["per_dimension"] == {"code": 1}

    patched = client.patch(
        f"/api/v1/evals/tasks/{task_id}", headers=AUTH, json={"title": "改名后"}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "改名后"

    deleted = client.delete(f"/api/v1/evals/tasks/{task_id}", headers=AUTH)
    assert deleted.status_code == 204

    # 软删后：默认查不到，但历史可追溯（include_archived=True 仍可见）
    assert client.get(f"/api/v1/evals/tasks/{task_id}", headers=AUTH).status_code == 404
    still_there = client.get(
        "/api/v1/evals/tasks", headers=AUTH, params={"include_archived": True}
    )
    assert still_there.json()["total"] == 1


def test_invalid_dimension_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/evals/tasks",
        headers=AUTH,
        json={"dimension": "nope", "title": "x", "prompt": "y"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_viewer_cannot_write(client: TestClient) -> None:
    r = client.post(
        "/api/v1/evals/tasks/seed", headers=VIEWER_AUTH
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_write_requires_auth(client: TestClient) -> None:
    assert client.post("/api/v1/evals/tasks/seed").status_code == 401


# ---------------------------------------------------------------------------
# ② 运行异步：POST 立即返回 run_id，不在请求里执行
# ---------------------------------------------------------------------------


def test_create_run_returns_queued_before_execution(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/v1/evals/tasks/seed", headers=AUTH)
    called: list[tuple[str, str]] = []

    async def fake_background(run_id: str, provider_name: str) -> None:
        called.append((run_id, provider_name))

    monkeypatch.setattr(evals_router, "_run_in_background", fake_background)

    r = client.post(
        "/api/v1/evals/runs", headers=AUTH, json={"models": ["gpt-4o-mini"]}
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["task_count"] == 20
    assert body["result_count"] == 0
    assert body["id"]
    # 后台任务被登记（而不是在请求里跑完）
    assert called == [(body["id"], "fake")]
    detail = client.get(f"/api/v1/evals/runs/{body['id']}", headers=AUTH).json()
    assert detail["results"] == []


def test_run_without_question_bank_rejected(client: TestClient) -> None:
    r = client.post("/api/v1/evals/runs", headers=AUTH, json={"models": ["m1"]})
    assert r.status_code == 422
    assert "题库为空" in r.json()["error"]["message"]


def test_run_with_unknown_task_returns_404(client: TestClient) -> None:
    r = client.post(
        "/api/v1/evals/runs",
        headers=AUTH,
        json={"models": ["m1"], "task_ids": ["does-not-exist"]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_run_requires_models(client: TestClient) -> None:
    r = client.post("/api/v1/evals/runs", headers=AUTH, json={"models": []})
    assert r.status_code == 422


def test_unsupported_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        provider_for("openai")


# ---------------------------------------------------------------------------
# ③ 结果字段 + 执行语义
# ---------------------------------------------------------------------------


async def _create_run(repo: FakeEvalRepo, *, models: list[str], cap: float | None = None):
    from app.modules.evals import service as svc
    from app.modules.evals.schemas import RunCreate

    return await svc.create_run(
        repo,
        owner=OWNER,
        payload=RunCreate(models=models, cost_cap_cny=cap),
        now=NOW,
    )


async def test_execute_run_records_all_fields(repo: FakeEvalRepo) -> None:
    await _seed(repo)
    run = await _create_run(repo, models=["model-a", "model-b"])
    provider = FakeProvider()

    finished = await execute_run(repo, run, await _tasks(repo), provider, now=NOW)

    assert finished.status == "succeeded"
    assert finished.error_code is None
    assert finished.result_count == 40  # 20 题 × 2 模型

    rows = await repo.list_results(run.id)
    assert len(rows) == 40
    for row in rows:
        assert row.model in {"model-a", "model-b"}
        assert row.score > 0
        assert row.cost_usd > 0
        assert row.latency_ms > 0
        assert row.raw_ref.startswith("fake://")
        assert row.cached is False


async def _tasks(repo: FakeEvalRepo):
    return await repo.list_tasks(limit=1000)


async def test_cache_hit_skips_provider_and_costs_nothing(repo: FakeEvalRepo) -> None:
    await _seed(repo)
    tasks = await _tasks(repo)
    provider = FakeProvider()

    run1 = await _create_run(repo, models=["model-a"])
    await execute_run(repo, run1, tasks, provider, now=NOW)
    calls_after_first = provider.calls
    assert calls_after_first == 20

    run2 = await _create_run(repo, models=["model-a"])
    second = await execute_run(repo, run2, tasks, provider, now=NOW + timedelta(days=1))

    assert provider.calls == calls_after_first, "7 天内同 model+同题不应再调 provider"
    assert second.cache_hits == 20
    assert float(second.cost_usd) == 0.0
    assert all(r.cached for r in await repo.list_results(run2.id))

    # 超过 7 天缓存失效，重新调用
    run3 = await _create_run(repo, models=["model-a"])
    third = await execute_run(repo, run3, tasks, provider, now=NOW + timedelta(days=8))
    assert provider.calls > calls_after_first
    assert third.cache_hits == 0


async def test_cost_cap_aborts_run(repo: FakeEvalRepo) -> None:
    await _seed(repo)
    run = await _create_run(repo, models=["expensive"], cap=0.5)
    provider = FakeProvider(cost_per_call_usd=1.0)  # 约 7.2 元/次，远超 0.5 元上限

    finished = await execute_run(repo, run, await _tasks(repo), provider, now=NOW)

    assert finished.status == "aborted"
    assert finished.error_code == "cost_cap_exceeded"
    assert "上限" in finished.error_message
    # 超限那一笔也已经花钱，必须入库（否则账对不上）
    assert finished.result_count == 1
    assert provider.calls == 1


async def test_provider_error_marks_run_failed(repo: FakeEvalRepo) -> None:
    await _seed(repo)

    class BoomProvider:
        async def score(self, *, model: str, task: EvalTask) -> ProviderResult:
            raise RuntimeError("upstream 502")

    run = await _create_run(repo, models=["model-a"])
    finished = await execute_run(repo, run, await _tasks(repo), BoomProvider(), now=NOW)

    assert finished.status == "failed"
    assert finished.error_code == "provider_error"
    assert "RuntimeError" in finished.error_message


async def test_cache_key_changes_when_task_updated(repo: FakeEvalRepo) -> None:
    """改题必须让缓存失效，否则会拿旧题的分数评新题。"""
    task = EvalTask(
        id="t1",
        dimension="code",
        title="x",
        prompt="y",
        rubric=[],
        max_score=100,
        weight=1,
        active=True,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    before = cache_key("m", task.id, f"{task.dimension}:{task.max_score}:{task.updated_at}")
    task.updated_at = NOW + timedelta(minutes=1)
    after = cache_key("m", task.id, f"{task.dimension}:{task.max_score}:{task.updated_at}")
    assert before != after


# ---------------------------------------------------------------------------
# W08-I02 · 8 维雷达聚合
#
# 验收映射：
# ① 8 个固定维度 → test_radar_endpoint_returns_eight_dimensions
# ② score / cost / p50_latency / samples → test_aggregate_averages_score_cost_and_p50
# ③ 缺样本返回 insufficient 而不是 0 → test_aggregate_*_insufficient
# ④ 聚合是纯函数 → 直接对 aggregate_radar 断言，不经过 HTTP
# ⑤ 响应 < 200ms → test_radar_aggregation_is_fast（100 样本单次聚合）
# ---------------------------------------------------------------------------

WEEK = "2026-W42"  # 2026-10-12（周一）~ 2026-10-18（周日）
WEEK_START = datetime(2026, 10, 12, 0, 0, 0)
WEEK_END = datetime(2026, 10, 19, 0, 0, 0)
IN_WEEK = datetime(2026, 10, 14, 9, 30, 0)


def _put_sample(
    repo: FakeEvalRepo,
    *,
    dimension: str,
    model: str,
    score: float,
    cost: float,
    latency: int,
    at: datetime = IN_WEEK,
    cached: bool = False,
) -> None:
    """往 fake repo 里塞一条"某题某模型"的结果，题目表补上对应维度。"""
    task_id = f"task-{dimension}"
    if task_id not in repo.tasks:
        repo.tasks[task_id] = EvalTask(
            id=task_id,
            dimension=dimension,
            title=f"{dimension} 题",
            prompt="p",
            rubric=[],
            max_score=100,
            weight=1,
            active=True,
            archived_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
    repo.results.append(
        EvalResult(
            id=f"r{len(repo.results)}",
            run_id="run-1",
            task_id=task_id,
            model=model,
            score=score,
            cost_usd=cost,
            latency_ms=latency,
            raw_ref="",
            cached=cached,
            cache_key=f"k{len(repo.results)}",
            created_at=at,
        )
    )


def test_parse_week_accepts_iso_week_date_and_default() -> None:
    window = parse_week(WEEK, now=NOW)
    assert window.label == "2026-W42"
    assert window.start == WEEK_START
    assert window.end == WEEK_END
    # 写成"该周内任意一天"也应该落到同一个周
    assert parse_week("2026-10-14", now=NOW).start == WEEK_START
    # 小写 w、无分隔符都要能认
    assert parse_week("2026w42", now=NOW).label == "2026-W42"
    assert parse_week("2026W42", now=NOW).label == "2026-W42"
    # 留空 = NOW 所在周
    assert parse_week(None, now=NOW).label == "2026-W42"
    assert parse_week("", now=NOW).label == "2026-W42"


def test_parse_week_rejects_garbage() -> None:
    for bad in ("not-a-week", "2026-W99", "2026-W0", "2026-13-01", "W42"):
        with pytest.raises(ValidationError):
            parse_week(bad, now=NOW)


def test_aggregate_marks_missing_dimension_insufficient_not_zero() -> None:
    """空样本必须是 insufficient + score=None：0 分会被误读成"这个维度很差"。"""
    out = aggregate_radar([], window=parse_week(WEEK, now=NOW))

    assert out["samples"] == 0
    assert out["total_cost_usd"] == 0.0
    assert len(out["dimensions"]) == 8
    for dim in out["dimensions"]:
        assert dim["status"] == "insufficient", dim["dimension"]
        assert dim["score"] is None, dim["dimension"]
        assert dim["p50_latency"] is None, dim["dimension"]
        assert dim["samples"] == 0
        assert dim["label"], "每个维度都要有可展示的中文名"


def test_aggregate_averages_score_cost_and_p50() -> None:
    samples = [
        RadarSample("code", "m1", 80, 0.002, 100),
        RadarSample("code", "m1", 90, 0.002, 200),
        # 缓存命中：分数有效但 cost=0，摊薄后每样本成本才会被拉低
        RadarSample("code", "m2", 60, 0.0, 300, cached=True),
    ]
    out = aggregate_radar(samples, window=parse_week(WEEK, now=NOW))
    code = next(d for d in out["dimensions"] if d["dimension"] == "code")

    assert code["status"] == "ok"
    assert code["samples"] == 3
    assert code["score"] == 76.67  # (80+90+60)/3
    assert code["p50_latency"] == 200
    assert code["cost"] == round(0.004 / 3, 6)
    assert code["models"]["m1"]["score"] == 85.0
    assert code["models"]["m2"]["samples"] == 1
    # 其余 7 维没有样本，仍然是 insufficient
    assert sum(1 for d in out["dimensions"] if d["status"] == "insufficient") == 7


def test_aggregate_p50_handles_even_sample_count() -> None:
    samples = [
        RadarSample("vision", "m1", 70, 0.001, 100),
        RadarSample("vision", "m1", 70, 0.001, 202),
    ]
    out = aggregate_radar(samples, window=parse_week(WEEK, now=NOW))
    vision = next(d for d in out["dimensions"] if d["dimension"] == "vision")
    assert vision["p50_latency"] == 151  # (100+202)/2


def test_aggregate_keeps_requested_models_as_insufficient() -> None:
    """指定了模型但该模型本周没测 → insufficient，而不是把它从轴上删掉。"""
    out = aggregate_radar(
        [RadarSample("code", "m1", 80, 0.002, 100)],
        window=parse_week(WEEK, now=NOW),
        models=["m1", "m9"],
    )
    code = next(d for d in out["dimensions"] if d["dimension"] == "code")
    assert code["models"]["m9"]["status"] == "insufficient"
    assert code["models"]["m9"]["score"] is None
    assert out["models"] == ["m1", "m9"]


def test_aggregate_reports_unknown_dimension_instead_of_silent_drop() -> None:
    samples = [
        RadarSample("code", "m1", 80, 0.002, 100),
        RadarSample("legacy_dim", "m1", 99, 0.002, 100),
    ]
    out = aggregate_radar(samples, window=parse_week(WEEK, now=NOW))
    assert out["unknown_dimensions"] == ["legacy_dim"]
    assert out["samples"] == 1, "未知维度不计入 8 轴样本数"
    assert len(out["dimensions"]) == 8


def test_radar_endpoint_returns_eight_dimensions(client: TestClient, repo: FakeEvalRepo) -> None:
    _put_sample(repo, dimension="code", model="m1", score=88, cost=0.002, latency=120)
    _put_sample(repo, dimension="vision", model="m1", score=72, cost=0.004, latency=260)

    r = client.get(f"/api/v1/evals/radar?week={WEEK}", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["week"] == "2026-W42"
    assert body["week_start"] == "2026-10-12"
    assert body["week_end"] == "2026-10-18"
    assert body["models"] == ["m1"]
    assert body["samples"] == 2
    assert len(body["dimensions"]) == 8

    dims = {d["dimension"]: d for d in body["dimensions"]}
    assert dims["code"]["score"] == 88.0
    assert dims["code"]["status"] == "ok"
    assert dims["code"]["p50_latency"] == 120
    assert dims["code"]["cost"] == 0.002
    assert dims["tool_call"]["status"] == "insufficient"
    assert dims["tool_call"]["score"] is None


def test_radar_endpoint_ignores_samples_outside_week(
    client: TestClient, repo: FakeEvalRepo
) -> None:
    _put_sample(
        repo,
        dimension="code",
        model="m1",
        score=90,
        cost=0.002,
        latency=100,
        at=datetime(2026, 10, 5, 10, 0),  # 上一周（W41）的样本
    )

    this_week = client.get(f"/api/v1/evals/radar?week={WEEK}", headers=AUTH).json()
    assert this_week["samples"] == 0
    assert all(d["status"] == "insufficient" for d in this_week["dimensions"])

    last_week = client.get("/api/v1/evals/radar?week=2026-W41", headers=AUTH).json()
    assert last_week["samples"] == 1
    assert last_week["week_start"] == "2026-10-05"


def test_radar_endpoint_filters_by_requested_models(
    client: TestClient, repo: FakeEvalRepo
) -> None:
    _put_sample(repo, dimension="code", model="m1", score=80, cost=0.002, latency=100)
    _put_sample(repo, dimension="code", model="m2", score=90, cost=0.002, latency=100)

    body = client.get(
        f"/api/v1/evals/radar?week={WEEK}&models=m1", headers=AUTH
    ).json()

    assert body["models"] == ["m1"]
    code = next(d for d in body["dimensions"] if d["dimension"] == "code")
    assert code["score"] == 80.0
    assert list(code["models"]) == ["m1"], "未请求的模型不应出现在轴上"


def test_radar_endpoint_rejects_bad_week(client: TestClient) -> None:
    r = client.get("/api/v1/evals/radar?week=oops", headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_radar_endpoint_requires_read_scope(client: TestClient) -> None:
    assert client.get(f"/api/v1/evals/radar?week={WEEK}").status_code in (401, 403)


def test_radar_aggregation_is_fast() -> None:
    """20 题 × 5 模型 = 100 行，聚合必须远低于 200ms 的验收线。"""
    import time

    samples = [
        RadarSample(DIMENSIONS[i % 8], f"m{i % 5}", 60 + (i % 40), 0.002, 100 + i)
        for i in range(100)
    ]
    window = parse_week(WEEK, now=NOW)

    started = time.perf_counter()
    out = aggregate_radar(samples, window=window)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert out["samples"] == 100
    assert elapsed_ms < 200, f"聚合耗时 {elapsed_ms:.1f} ms，超过 200ms 验收线"
