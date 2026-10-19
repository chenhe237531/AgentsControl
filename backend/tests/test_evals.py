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
