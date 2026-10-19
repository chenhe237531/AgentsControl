"""W06 执行面测试。

原则（见 plan/W06.md 各 issue 验收）：
- 不依赖真实 Postgres/Redis/Orca：存储用内存 fake，tasks 用 FakeTasksGateway，
  Orca 探测 mock subprocess，worktree 用临时 git 仓库；
- 端到端语义（claim→complete 的 fake worker）必须在 HTTP 层跑通。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_token
from app.main import create_app
from app.modules.execution import harvest as exec_harvest
from app.modules.execution import orca as exec_orca
from app.modules.execution import protocol as proto
from app.modules.execution import service as exec_service
from app.modules.execution.protocol import LeasedTask
from app.modules.execution.repository import EventRecord, SessionRow, WorkerInfo
from app.modules.execution.worktree import (
    FLEET_TASK_FILENAME,
    GitCommandError,
    WorktreeExistsError,
    WorktreeManager,
    fleet_task_idem_key,
)

# 包 __init__ 把 `router` 导出为 APIRouter 实例（路由注册点约定），
# 测试要拿的是 router *模块*（依赖装配函数挂在那上面）。
exec_router = importlib.import_module("app.modules.execution.router")

AUTH = {
    "Authorization": "Bearer "
    + create_token("owner-1", "access", extra={"role": "owner"})
}


def utcnow() -> datetime:
    return datetime.now(UTC)


# ================================================================ fakes

class FakeWorkerStore:
    def __init__(self) -> None:
        self.rows: dict[str, WorkerInfo] = {}
        self.commits = 0

    async def upsert(self, info: WorkerInfo) -> WorkerInfo:
        self.rows[info.id] = info
        return info

    async def touch(self, worker_id: str) -> None:
        row = self.rows.get(worker_id)
        if row is None:
            self.rows[worker_id] = WorkerInfo(id=worker_id, last_seen_at=utcnow())
        else:
            self.rows[worker_id] = replace(row, last_seen_at=utcnow())

    async def get(self, worker_id: str) -> WorkerInfo | None:
        return self.rows.get(worker_id)

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class FakeTasksGateway:
    queue: list[LeasedTask] = field(default_factory=list)
    renews: list[tuple[str, str]] = field(default_factory=list)
    completed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    released: list[tuple[str, str]] = field(default_factory=list)
    lease_calls: list[dict[str, Any]] = field(default_factory=list)

    async def lease(
        self,
        *,
        capabilities: list[str],
        slots: int,
        machine: str | None,
        worker: str | None = None,
    ):
        self.lease_calls.append(
            {"capabilities": capabilities, "slots": slots, "machine": machine, "worker": worker}
        )
        return self.queue.pop(0) if self.queue else None

    async def renew_lease(self, task_id: str, worker: str) -> datetime:
        self.renews.append((task_id, worker))
        return utcnow() + timedelta(seconds=90)

    async def release(self, task_id: str, worker: str) -> None:
        self.released.append((task_id, worker))

    async def complete(self, task_id: str, worker: str, result: dict[str, Any]) -> None:
        self.completed.append({"task_id": task_id, "worker": worker, "result": result})

    async def fail(
        self,
        task_id: str,
        worker: str,
        error_type: str,
        retryable: bool,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.failed.append(
            {
                "task_id": task_id,
                "worker": worker,
                "error_type": error_type,
                "retryable": retryable,
                "context": context,
            }
        )


class FakeEventStore:
    def __init__(self) -> None:
        self.rows: list[EventRecord] = []
        self._next_id = 1
        self.commits = 0

    async def append(
        self,
        *,
        task_id: str,
        kind: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        worker_id: str | None = None,
        source: str = "service",
    ) -> EventRecord:
        record = EventRecord(
            id=self._next_id,
            task_id=task_id,
            kind=kind,
            session_id=session_id,
            worker_id=worker_id,
            source=source,
            payload=payload,
            created_at=utcnow(),
        )
        self._next_id += 1
        self.rows.append(record)
        return record

    async def list_for_task(self, task_id: str, *, limit: int = 20) -> list[EventRecord]:
        return [r for r in self.rows if r.task_id == task_id][:limit]

    async def list_for_session(self, session_id: str, *, limit: int = 20) -> list[EventRecord]:
        return [r for r in self.rows if r.session_id == session_id][:limit]

    async def commit(self) -> None:
        self.commits += 1

    def kinds(self, task_id: str) -> list[str]:
        return [r.kind for r in self.rows if r.task_id == task_id]


class FakeSessionStore:
    def __init__(self) -> None:
        self.rows: dict[str, SessionRow] = {}
        self.commits = 0

    async def create(self, row: SessionRow) -> SessionRow:
        self.rows[row.id] = row
        return row

    async def get(self, session_id: str) -> SessionRow | None:
        return self.rows.get(session_id)

    async def find_active_by_name(self, issue: str, slug: str) -> SessionRow | None:
        for row in self.rows.values():
            if row.issue == issue and row.slug == slug and row.state != "recycled":
                return row
        return None

    async def list_sessions(
        self,
        *,
        state: str | None = None,
        device_id: str | None = None,
        objective_id: str | None = None,
        runtime: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRow]:
        rows = sorted(
            self.rows.values(),
            key=lambda r: (r.created_at or utcnow(), r.id),
            reverse=True,
        )
        rows = [
            r
            for r in rows
            if (state is None or r.state == state)
            and (device_id is None or r.device_id == device_id)
            and (objective_id is None or r.objective_id == objective_id)
            and (runtime is None or r.runtime == runtime)
        ]
        return rows[offset : offset + limit]

    async def update(self, session_id: str, **fields: Any) -> SessionRow | None:
        row = self.rows.get(session_id)
        if row is None:
            return None
        updated = replace(row, **fields, updated_at=utcnow())
        self.rows[session_id] = updated
        return updated

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture()
def workers_store() -> FakeWorkerStore:
    return FakeWorkerStore()


@pytest.fixture()
def events_store() -> FakeEventStore:
    return FakeEventStore()


@pytest.fixture()
def gateway() -> FakeTasksGateway:
    return FakeTasksGateway()


@pytest.fixture()
def sessions_store() -> FakeSessionStore:
    return FakeSessionStore()


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """最小可用的真实 git 仓库（git init + 初始提交），绝不碰主仓。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# tmp\n", encoding="utf-8")
    git_commit(repo, "init")
    return repo


def git_commit(cwd: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture()
def sessions_client(
    tmp_repo: Path,
    workers_store: FakeWorkerStore,
    events_store: FakeEventStore,
    gateway: FakeTasksGateway,
    sessions_store: FakeSessionStore,
) -> TestClient:
    """带会话 fake 与真实临时 git 仓库的 App（worktree 操作走真 git 子进程）。"""
    app = create_app()
    app.dependency_overrides[exec_router.get_worker_store] = lambda: workers_store
    app.dependency_overrides[exec_router.get_event_store] = lambda: events_store
    app.dependency_overrides[exec_router.get_tasks_gateway] = lambda: gateway
    app.dependency_overrides[exec_router.get_session_store] = lambda: sessions_store
    app.dependency_overrides[exec_router.get_worktree_manager] = lambda: WorktreeManager(
        tmp_repo
    )
    return TestClient(app)


@pytest.fixture()
def client(
    workers_store: FakeWorkerStore, events_store: FakeEventStore, gateway: FakeTasksGateway
) -> TestClient:
    """挂上全部 fake 依赖的 App；sessions/orca 相关依赖在各自 issue 的测试里补充覆盖。"""
    app = create_app()
    app.dependency_overrides[exec_router.get_worker_store] = lambda: workers_store
    app.dependency_overrides[exec_router.get_event_store] = lambda: events_store
    app.dependency_overrides[exec_router.get_tasks_gateway] = lambda: gateway
    return TestClient(app)


def queued_task(task_id: str = "T-0412") -> LeasedTask:
    return LeasedTask(
        task_id=task_id,
        payload={"objective": "O-001", "capability": "code.build.feature"},
        lease_expires_at=utcnow() + timedelta(seconds=90),
    )


# ================================================================ W06-I01 · Worker 协议

class TestWorkerProtocol:
    def test_claim_returns_task_and_writes_event(self, client, gateway, events_store, workers_store):
        gateway.queue.append(queued_task())
        r = client.post(
            "/api/v1/execution/workers/claim",
            headers=AUTH,
            json={
                "worker_id": "w-mac-mini-1",
                "capabilities": ["orca", "has_gui"],
                "concurrency_slots": 4,
                "machine": "mac-mini-m4",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["task"]["task_id"] == "T-0412"
        assert body["worker_id"] == "w-mac-mini-1"
        assert body["lease_expires_at"]
        # 领活语义：任务派给了正确的能力标签与机型
        assert gateway.lease_calls == [
            {"capabilities": ["orca", "has_gui"], "slots": 4, "machine": "mac-mini-m4"}
        ]
        assert events_store.kinds("T-0412") == ["claimed"]
        assert workers_store.rows["w-mac-mini-1"].capabilities == ("orca", "has_gui")

    def test_claim_without_task_returns_204(self, client, events_store):
        r = client.post(
            "/api/v1/execution/workers/claim",
            headers=AUTH,
            json={"capabilities": ["docker"], "concurrency_slots": 1},
        )
        assert r.status_code == 204
        assert r.content == b""
        assert events_store.rows == []

    def test_claim_generates_worker_id_when_absent(self, client, gateway):
        gateway.queue.append(queued_task("T-2"))
        r = client.post(
            "/api/v1/execution/workers/claim", headers=AUTH, json={"concurrency_slots": 2}
        )
        assert r.status_code == 200
        assert r.json()["worker_id"].startswith("w-")

    def test_claim_validates_slots(self, client):
        r = client.post(
            "/api/v1/execution/workers/claim",
            headers=AUTH,
            json={"concurrency_slots": 0},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"

    def test_heartbeat_renews_lease(self, client, gateway):
        r = client.post(
            "/api/v1/execution/workers/w-1/heartbeat",
            headers=AUTH,
            json={"task_id": "T-0412", "progress": 42.5, "step": "write_tests"},
        )
        assert r.status_code == 200
        assert r.json()["task_id"] == "T-0412"
        assert gateway.renews == [("T-0412", "w-1")]

    def test_heartbeat_pure_renew_writes_no_event(self, client, events_store):
        r = client.post(
            "/api/v1/execution/workers/w-1/heartbeat",
            headers=AUTH,
            json={"task_id": "T-0412"},
        )
        assert r.status_code == 200
        assert events_store.rows == []

    def test_complete_requires_result(self, client):
        r = client.post(
            "/api/v1/execution/workers/w-1/complete",
            headers=AUTH,
            json={"task_id": "T-0412"},  # 缺 result → 422
        )
        assert r.status_code == 422

    def test_complete_writes_events_with_cost_per_model(self, client, gateway, events_store):
        r = client.post(
            "/api/v1/execution/workers/w-1/complete",
            headers=AUTH,
            json={
                "task_id": "T-0412",
                "result": {
                    "artifacts": [{"name": "pr-diff", "ref": "cos://bucket/x.patch"}],
                    "cost": {"total": 0.27, "by_model": {"claude": 0.18, "gpt": 0.09}},
                    "elapsed_ms": 42000,
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["events_written"] == 3  # completed + 2 条分模型 cost
        # 交货语义：产物引用 + 成本进入 tasks 模块与事件流
        assert gateway.completed[0]["result"]["cost"]["by_model"] == {"claude": 0.18, "gpt": 0.09}
        assert events_store.kinds("T-0412") == ["completed", "cost", "cost"]

    def test_fail_requires_retryable_flag(self, client):
        r = client.post(
            "/api/v1/execution/workers/w-1/fail",
            headers=AUTH,
            json={"task_id": "T-0412", "error_type": "timeout"},
        )
        assert r.status_code == 422  # retryable 是语义必需，不是可选

    def test_fail_records_retryability(self, client, gateway, events_store):
        r = client.post(
            "/api/v1/execution/workers/w-1/fail",
            headers=AUTH,
            json={
                "task_id": "T-0412",
                "error_type": "upstream_5xx",
                "retryable": True,
                "context": {"attempt": 2},
            },
        )
        assert r.status_code == 200
        assert r.json() == {"task_id": "T-0412", "status": "failed", "retryable": True}
        assert gateway.failed[0]["retryable"] is True
        assert events_store.kinds("T-0412") == ["failed"]

    def test_fake_worker_end_to_end_claim_to_complete(self, client, gateway, events_store):
        """I01 验收 ⑤：fake worker 端到端跑通 claim → heartbeat → complete。"""
        gateway.queue.append(queued_task("T-e2e"))

        claimed = client.post(
            "/api/v1/execution/workers/claim",
            headers=AUTH,
            json={"worker_id": "w-e2e", "capabilities": ["orca"], "concurrency_slots": 1},
        )
        assert claimed.status_code == 200
        task_id = claimed.json()["task"]["task_id"]
        worker_id = claimed.json()["worker_id"]

        beat = client.post(
            f"/api/v1/execution/workers/{worker_id}/heartbeat",
            headers=AUTH,
            json={"task_id": task_id, "progress": 10, "step": "bootstrap"},
        )
        assert beat.status_code == 200

        done = client.post(
            f"/api/v1/execution/workers/{worker_id}/complete",
            headers=AUTH,
            json={
                "task_id": task_id,
                "result": {"artifacts": [], "cost": {"total": 0.0}},
            },
        )
        assert done.status_code == 200
        assert events_store.kinds(task_id) == ["claimed", "heartbeat", "completed"]
        assert gateway.completed[0]["task_id"] == task_id

    def test_worker_endpoints_require_auth(self, client):
        r = client.post("/api/v1/execution/workers/claim", json={})
        assert r.status_code == 401


class TestTasksUnavailable:
    def test_claim_reports_502_when_tasks_module_missing(self, workers_store, events_store, monkeypatch):
        """tasks.service 缺失/破坏时的行为：明确 502 tasks_unavailable，绝不降级猜测。

        W03 已合入 main，真实模块存在；这里用一个缺函数的假模块替换它，
        验证的仍是同一条防线：网关拒绝在契约不满足时瞎猜。
        """
        import sys
        import types

        import app.modules.tasks as tasks_pkg

        app = create_app()
        app.dependency_overrides[exec_router.get_worker_store] = lambda: workers_store
        app.dependency_overrides[exec_router.get_event_store] = lambda: events_store
        app.dependency_overrides[exec_router.get_tasks_gateway] = (
            lambda: proto.TasksServiceGateway()
        )
        broken = types.ModuleType("app.modules.tasks.service")  # 无任何公开函数
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", broken)
        monkeypatch.setattr(tasks_pkg, "service", broken, raising=False)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/execution/workers/claim",
                headers=AUTH,
                json={"capabilities": ["orca"], "concurrency_slots": 1},
            )
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "tasks_unavailable"


class TestTasksServiceGatewayContract:
    """网关 ↔ tasks.service（main 上 W03 真实公开 API）的适配契约。

    真实签名：claim(session, body) -> (Task, lease_seconds) | None；
    heartbeat/complete/fail(session, task_id, *, lease_token, ...)。
    租约栅栏靠 lease_token，由网关在 claim 时记录、续约/交货/失败时携带。
    """

    @staticmethod
    def _install_stub(monkeypatch, claim_return: Any) -> dict[str, list]:
        import sys
        import types
        import uuid as uuid_mod

        import app.modules.tasks as tasks_pkg

        calls: dict[str, list] = {"claim": [], "heartbeat": [], "complete": [], "fail": []}
        state: dict[str, object] = {"task": None}

        def _task_obj(task_id, *, token=None, expires=None):
            return types.SimpleNamespace(
                id=uuid_mod.UUID(task_id),
                payload={"k": 1},
                lease_token=uuid_mod.UUID(token) if token else None,
                leased_until=expires or utcnow() + timedelta(seconds=90),
            )

        state["_task_obj"] = _task_obj
        mod = types.ModuleType("app.modules.tasks.service")

        async def claim(session, body, *, settings=None):
            calls["claim"].append(body)
            if claim_return is None:
                return None
            task_id, token = claim_return
            task = _task_obj(task_id, token=token)
            state["task"] = task
            return task, 90

        async def heartbeat(session, task_id, *, lease_token, settings=None):
            calls["heartbeat"].append({"task_id": str(task_id), "lease_token": str(lease_token)})
            task = state["task"]
            return types.SimpleNamespace(
                id=task_id, leased_until=utcnow() + timedelta(seconds=90)
            )

        async def complete(session, task_id, *, lease_token, result):
            calls["complete"].append({"task_id": str(task_id), "result": result})
            return state["task"]

        async def fail(session, task_id, *, lease_token, error, retryable, settings=None):
            calls["fail"].append({"task_id": str(task_id), "error": error, "retryable": retryable})
            return state["task"]

        mod.claim = claim
        mod.heartbeat = heartbeat
        mod.complete = complete
        mod.fail = fail
        # 必须同时替换 sys.modules 条目和包属性：`from app.modules.tasks import service`
        # 优先取包属性，只换 sys.modules 会被真实模块抢先绑定而失效。
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", mod)
        monkeypatch.setattr(tasks_pkg, "service", mod, raising=False)

        class _NullSession:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(proto, "_open_session", lambda: _NullSession())
        proto._lease_tokens.clear()
        return calls

    @pytest.mark.asyncio
    async def test_claim_maps_to_leased_task(self, monkeypatch):
        import uuid as uuid_mod

        task_id, token = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        calls = self._install_stub(monkeypatch, (task_id, token))
        gw = proto.TasksServiceGateway()
        leased = await gw.lease(
            capabilities=["orca"], slots=1, machine="mac-studio", worker="w-1"
        )
        assert leased is not None
        assert leased.task_id == task_id
        assert leased.payload == {"k": 1}
        # ClaimIn 映射：device_id 取 worker，caps 为 {能力: True}
        assert calls["claim"][0].device_id == "w-1"
        assert calls["claim"][0].caps == {"orca": True}
        assert proto._lease_tokens[task_id] == token

    @pytest.mark.asyncio
    async def test_claim_none_returns_none(self, monkeypatch):
        self._install_stub(monkeypatch, None)
        gw = proto.TasksServiceGateway()
        assert await gw.lease(capabilities=[], slots=1, machine=None) is None

    @pytest.mark.asyncio
    async def test_claim_rejects_garbage(self, monkeypatch):
        import sys
        import types

        import app.modules.tasks as tasks_pkg

        # 返回缺 id 的对象 → 拒绝，不把脏数据带进事件表
        mod = types.ModuleType("app.modules.tasks.service")

        async def claim(session, body, *, settings=None):
            return object(), 90  # 无 id/payload 属性

        async def heartbeat(session, task_id, *, lease_token):
            return None

        async def complete(session, task_id, *, lease_token, result):
            return None

        async def fail(session, task_id, *, lease_token, error, retryable):
            return None

        mod.claim, mod.heartbeat, mod.complete, mod.fail = claim, heartbeat, complete, fail
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", mod)
        monkeypatch.setattr(tasks_pkg, "service", mod, raising=False)
        gw = proto.TasksServiceGateway()
        with pytest.raises(proto.TasksUnavailableError):
            await gw.lease(capabilities=[], slots=1, machine=None)

    @pytest.mark.asyncio
    async def test_renew_uses_token_from_claim(self, monkeypatch):
        import uuid as uuid_mod

        task_id, token = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        calls = self._install_stub(monkeypatch, (task_id, token))
        gw = proto.TasksServiceGateway()
        await gw.lease(capabilities=[], slots=1, machine=None)
        expires = await gw.renew_lease(task_id, worker="w-1")
        assert isinstance(expires, datetime)
        assert calls["heartbeat"][0]["lease_token"] == token

    @pytest.mark.asyncio
    async def test_renew_without_token_is_lease_lost(self, monkeypatch):
        from app.core.errors import ConflictError

        self._install_stub(monkeypatch, None)
        gw = proto.TasksServiceGateway()
        with pytest.raises(ConflictError) as exc_info:
            await gw.renew_lease(str(__import__("uuid").uuid4()), worker="w-1")
        assert exc_info.value.code == "lease_lost"

    @pytest.mark.asyncio
    async def test_complete_and_fail_consume_token(self, monkeypatch):
        import uuid as uuid_mod

        from app.core.errors import ConflictError

        task_id, token = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        calls = self._install_stub(monkeypatch, (task_id, token))
        gw = proto.TasksServiceGateway()
        await gw.lease(capabilities=[], slots=1, machine=None)
        await gw.complete(task_id, worker="w-1", result={"artifacts": []})
        assert calls["complete"][0]["result"] == {"artifacts": []}
        with pytest.raises(ConflictError):  # 终态后 token 已清除
            await gw.complete(task_id, worker="w-1", result={})

    @pytest.mark.asyncio
    async def test_fail_carries_error_and_retryable(self, monkeypatch):
        import uuid as uuid_mod

        task_id, token = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        calls = self._install_stub(monkeypatch, (task_id, token))
        gw = proto.TasksServiceGateway()
        await gw.lease(capabilities=[], slots=1, machine=None)
        await gw.fail(task_id, worker="w-1", error_type="boom", retryable=True, context={"x": 1})
        assert calls["fail"][0]["error"].startswith("boom")
        assert calls["fail"][0]["retryable"] is True

    @pytest.mark.asyncio
    async def test_invalid_task_uuid_rejected(self, monkeypatch):
        self._install_stub(monkeypatch, None)
        gw = proto.TasksServiceGateway()
        with pytest.raises(proto.TasksUnavailableError):
            await gw.renew_lease("not-a-uuid", worker="w-1")

    @pytest.mark.asyncio
    async def test_missing_service_functions_raise_unavailable(self, monkeypatch):
        import sys
        import types

        import app.modules.tasks as tasks_pkg

        empty = types.ModuleType("app.modules.tasks.service")
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", empty)
        monkeypatch.setattr(tasks_pkg, "service", empty, raising=False)
        gw = proto.TasksServiceGateway()
        with pytest.raises(proto.TasksUnavailableError):
            await gw.lease(capabilities=[], slots=1, machine=None)


# ================================================================ W06-I02 · Orca probe

@dataclass
class FakeOrcaRunner:
    """Orca CLI 假体：按子命令名（args 中的精确元素）返回预置结果。

    delay>0 可模拟慢 harvest，验证「202 立即返回、收集后台执行」。
    """

    outputs: dict[str, exec_orca.RunnerResult]
    calls: list[list[str]] = field(default_factory=list)
    delay: float = 0.0

    async def run(
        self, args: list[str], *, timeout: float = 5.0
    ) -> exec_orca.RunnerResult:
        self.calls.append(args)
        if self.delay:
            await asyncio.sleep(self.delay)
        for key, result in self.outputs.items():
            if key in args:
                return result
        return exec_orca.RunnerResult(rc=0, stdout="", stderr="")


def healthy_runner() -> FakeOrcaRunner:
    return FakeOrcaRunner(
        outputs={
            "--version": exec_orca.RunnerResult(rc=0, stdout="orca 0.3.1 (build 42)", stderr=""),
            "agents": exec_orca.RunnerResult(
                rc=0, stdout='{"agents": ["claude-code", "opencode"]}', stderr=""
            ),
        }
    )


class TestOrcaProbe:
    async def test_probe_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: "/usr/local/bin/orca")
        result = await exec_orca.probe(
            healthy_runner(), worktree_root=str(tmp_path)
        )
        assert result.available is True
        assert result.version == "0.3.1"
        assert result.cli_path == "/usr/local/bin/orca"
        assert result.agents == ("claude-code", "opencode")
        assert result.worktree_root_writable is True
        assert result.error is None

    async def test_probe_reports_missing_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: None)
        result = await exec_orca.probe(
            healthy_runner(), worktree_root=str(tmp_path)
        )
        assert result.available is False
        assert "未找到 Orca CLI" in (result.error or "")

    async def test_probe_reports_format_change_explicitly(self, tmp_path, monkeypatch):
        """I02 验收 ③：版本输出格式变化 → 明确错误，不空指针。"""
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: "/bin/orca")
        runner = FakeOrcaRunner(
            outputs={
                "--version": exec_orca.RunnerResult(
                    rc=0, stdout="completely-unrecognizable-garbage", stderr=""
                )
            }
        )
        result = await exec_orca.probe(runner, worktree_root=str(tmp_path))
        assert result.available is False
        assert "Orca CLI 输出格式变了" in (result.error or "")

    async def test_probe_reports_version_command_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: "/bin/orca")
        runner = FakeOrcaRunner(
            outputs={
                "--version": exec_orca.RunnerResult(rc=2, stdout="", stderr="boom")
            }
        )
        result = await exec_orca.probe(runner, worktree_root=str(tmp_path))
        assert result.available is False
        assert "退出码 2" in (result.error or "")

    async def test_probe_rejects_unwritable_worktree_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: "/bin/orca")
        missing = tmp_path / "no-such-dir"
        result = await exec_orca.probe(
            healthy_runner(), worktree_root=str(missing)
        )
        assert result.available is False
        assert "worktree 根目录不可写" in (result.error or "")

    async def test_probe_degrades_gracefully_when_agents_unavailable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(exec_orca.shutil, "which", lambda _: "/bin/orca")
        runner = healthy_runner()
        runner.outputs["agents"] = exec_orca.RunnerResult(rc=1, stdout="", stderr="nope")
        result = await exec_orca.probe(runner, worktree_root=str(tmp_path))
        assert result.available is True  # agents 列举失败只降级
        assert result.agents == ()
        assert result.details.get("agents_error")


class TestOrcaHealthEndpoint:
    def test_health_serves_cached_probe_without_reprobing(self, monkeypatch):
        cached = exec_orca.OrcaProbeResult(
            available=True,
            checked_at=utcnow(),
            cli_path="/bin/orca",
            version="0.3.1",
            agents=("claude-code",),
            worktree_root="/x/.worktrees",
            worktree_root_writable=True,
        )
        probed = {"count": 0}

        import json as _json

        async def fake_cache_get(key):
            assert key == exec_orca.PROBE_CACHE_KEY
            return _json.dumps(cached.to_dict())

        async def fake_cache_set(key, value, ttl_seconds):
            assert ttl_seconds == exec_orca.PROBE_TTL_SECONDS

        async def fail_probe(*args, **kwargs):
            probed["count"] += 1
            raise AssertionError("命中缓存时不应重新探测")

        monkeypatch.setattr(exec_orca, "cache_get", fake_cache_get)
        monkeypatch.setattr(exec_orca, "cache_set", fake_cache_set)
        monkeypatch.setattr(exec_orca, "probe", fail_probe)
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/api/v1/execution/orca/health", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["version"] == "0.3.1"
        assert body["agents"] == ["claude-code"]
        assert probed["count"] == 0

    def test_health_probes_and_caches_on_cold_start(self, tmp_path, monkeypatch):
        fresh = exec_orca.OrcaProbeResult(
            available=False,
            checked_at=utcnow(),
            error="未找到 Orca CLI：PATH 中无 `orca`，无法派 Orca 类任务",
        )
        set_calls: list[tuple[str, int]] = []

        async def fake_cache_get(key):
            return None

        async def fake_cache_set(key, value, ttl_seconds):
            set_calls.append((key, ttl_seconds))

        async def fake_probe(*args, **kwargs):
            return fresh

        monkeypatch.setattr(exec_orca, "cache_get", fake_cache_get)
        monkeypatch.setattr(exec_orca, "cache_set", fake_cache_set)
        monkeypatch.setattr(exec_orca, "probe", fake_probe)
        monkeypatch.setattr(exec_orca, "REFRESHER_ENABLED", False)
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/api/v1/execution/orca/health", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert "未找到 Orca CLI" in body["error"]
        assert set_calls == [(exec_orca.PROBE_CACHE_KEY, exec_orca.PROBE_TTL_SECONDS)]

    def test_health_requires_auth(self, monkeypatch):
        app = create_app()
        with TestClient(app) as c:
            r = c.get("/api/v1/execution/orca/health")
        assert r.status_code == 401


# ================================================================ W06-I03 · worktree 生命周期

class TestWorktreeManager:
    """真 git 子进程 + 临时仓库（I03 验收 ④：不碰真实仓库）。"""

    async def test_create_writes_fleet_task_and_branch(self, tmp_repo):
        mgr = WorktreeManager(tmp_repo)
        info = await mgr.create(
            issue="W06I03",
            slug="fix-login",
            fleet_task={"task_id": "T-0412", "idem_key": "abc123"},
        )
        assert info.branch == "issue/W06I03-fix-login"
        path = Path(info.path)
        assert path == tmp_repo / ".worktrees" / "W06I03-fix-login"
        assert path.is_dir()
        on_disk = mgr.read_fleet_task(info.path)
        assert on_disk["task_id"] == "T-0412"
        assert on_disk["idem_key"] == "abc123"

    async def test_create_with_base_ref(self, tmp_repo):
        mgr = WorktreeManager(tmp_repo)
        info = await mgr.create(issue="W06I03", slug="on-main", base_ref="main")
        assert info.base_ref == "main"
        assert (Path(info.path) / "README.md").is_file()  # base_ref 的内容被检出

    async def test_duplicate_create_raises(self, tmp_repo):
        mgr = WorktreeManager(tmp_repo)
        await mgr.create(issue="W06I03", slug="dup")
        with pytest.raises(WorktreeExistsError):
            await mgr.create(issue="W06I03", slug="dup")

    async def test_fleet_task_file_is_not_agent_work(self, tmp_repo):
        """.fleet-task.json 是控制面元数据：不算未提交改动，干净回收不被拒。"""
        mgr = WorktreeManager(tmp_repo)
        info = await mgr.create(
            issue="W06I03", slug="clean", fleet_task={"task_id": "T-0412"}
        )
        assert (Path(info.path) / FLEET_TASK_FILENAME).is_file()
        assert await mgr.uncommitted_files(info.path) == []
        await mgr.remove(info.path)  # 无需 force
        assert not Path(info.path).exists()

    async def test_uncommitted_files_detects_agent_changes(self, tmp_repo):
        mgr = WorktreeManager(tmp_repo)
        info = await mgr.create(issue="W06I03", slug="dirty")
        assert await mgr.uncommitted_files(info.path) == []
        (Path(info.path) / "WIP.patch").write_text("x", encoding="utf-8")
        assert await mgr.uncommitted_files(info.path) == ["WIP.patch"]

    async def test_remove_refuses_dirty_worktree_until_forced(self, tmp_repo):
        mgr = WorktreeManager(tmp_repo)
        info = await mgr.create(issue="W06I03", slug="guard")
        (Path(info.path) / "WIP.patch").write_text("x", encoding="utf-8")
        with pytest.raises(GitCommandError):
            await mgr.remove(info.path)
        assert Path(info.path).exists()  # 拒绝时磁盘事实不动
        await mgr.remove(info.path, force=True)
        assert not Path(info.path).exists()

    async def test_recreate_after_remove_reuses_branch(self, tmp_repo):
        """回收后重派同名会话：分支被复用，其上的提交不丢。"""
        mgr = WorktreeManager(tmp_repo)
        first = await mgr.create(issue="W06I03", slug="again")
        (Path(first.path) / "feature.py").write_text("print('v1')\n", encoding="utf-8")
        git_commit(Path(first.path), "feat")
        await mgr.remove(first.path)

        second = await mgr.create(issue="W06I03", slug="again")
        assert second.branch == first.branch
        assert (Path(second.path) / "feature.py").read_text(encoding="utf-8") == "print('v1')\n"

    def test_fleet_task_idem_key_is_deterministic(self):
        assert fleet_task_idem_key("T-1", "/a") == fleet_task_idem_key("T-1", "/a")
        assert fleet_task_idem_key("T-1", "/a") != fleet_task_idem_key("T-2", "/a")
        assert fleet_task_idem_key("T-1", "/a") != fleet_task_idem_key("T-1", "/b")
        assert len(fleet_task_idem_key("T-1", "/a")) == 16


def create_session_body(**overrides) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": "T-0412",
        "issue": "W06I03",
        "slug": "fix-login",
        "capability": "code.build.feature",
        "device_id": "mac-mini-m4",
        "objective_id": "O-001",
        "prompt_ver": "v3",
        "skill_versions": {"repo": "a1b2c3"},
        "budget_remaining": {"usd": 5.0},
        "acceptance": {"cmd": "pytest -q", "must_pass": ["backend/tests/"]},
    }
    body.update(overrides)
    return body


class TestSessionLifecycle:
    def test_create_session_creates_worktree_and_fleet_task(
        self, sessions_client, events_store
    ):
        r = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        )
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "created"
        assert body["branch"] == "issue/W06I03-fix-login"
        assert body["runtime"] == "generic"

        wt = Path(body["worktree_path"])
        assert wt.is_dir()
        fleet = json.loads((wt / FLEET_TASK_FILENAME).read_text(encoding="utf-8"))
        assert fleet["task_id"] == "T-0412"
        assert fleet["session_id"] == body["id"]
        assert fleet["capability"] == "code.build.feature"
        assert fleet["acceptance"]["cmd"] == "pytest -q"
        assert fleet["idem_key"] == body["fleet_task"]["idem_key"]
        assert len(fleet["idem_key"]) == 16
        # 落事件供会话视图（I06）展示
        assert events_store.kinds("T-0412") == ["session.created"]

    def test_create_session_rejects_duplicate_active_session(self, sessions_client):
        first = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        )
        assert first.status_code == 201
        second = sessions_client.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(task_id="T-9999"),  # 同 issue+slug，不同 task
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "session_exists"

    def test_create_session_refuses_preexisting_worktree_dir(self, sessions_client, tmp_repo):
        """库里有会话但目录被外部占用（或反之）：绝不产生第二个 worktree。"""
        (tmp_repo / ".worktrees" / "W06I03-ghost").mkdir(parents=True)
        r = sessions_client.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(slug="ghost"),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "session_exists"

    def test_create_session_validates_slug_charset(self, sessions_client):
        r = sessions_client.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(slug="Bad_Slug"),
        )
        assert r.status_code == 422

    def test_create_session_requires_explicit_slug_when_task_id_not_slug_safe(
        self, sessions_client
    ):
        r = sessions_client.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(slug=None),  # task_id=T-0412 含大写
        )
        assert r.status_code == 422

    def test_create_session_defaults_slug_from_slug_safe_task_id(self, sessions_client):
        r = sessions_client.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(task_id="t0412", slug=None),
        )
        assert r.status_code == 201
        assert r.json()["slug"] == "t0412"
        assert r.json()["branch"] == "issue/W06I03-t0412"

    def test_recycle_clean_session(self, sessions_client, events_store):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        r = sessions_client.delete(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        assert r.status_code == 200
        assert r.json()["state"] == "recycled"
        assert not Path(created["worktree_path"]).exists()
        assert events_store.kinds("T-0412") == ["session.created", "session.recycled"]

    def test_recycle_dirty_session_returns_file_list(self, sessions_client):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        (Path(created["worktree_path"]) / "WIP.patch").write_text("wip\n", encoding="utf-8")
        r = sessions_client.delete(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "worktree_dirty"
        assert "WIP.patch" in err["details"]["files"]
        # 拒绝时 Agent 的工作原样保留
        assert Path(created["worktree_path"], "WIP.patch").is_file()

    def test_recycle_force_discards_dirty_worktree(self, sessions_client):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        (Path(created["worktree_path"]) / "WIP.patch").write_text("wip\n", encoding="utf-8")
        r = sessions_client.delete(
            f"/api/v1/execution/sessions/{created['id']}?force=true", headers=AUTH
        )
        assert r.status_code == 200
        assert r.json()["state"] == "recycled"
        assert not Path(created["worktree_path"]).exists()

    def test_recycle_is_idempotent(self, sessions_client, events_store):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        first = sessions_client.delete(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        second = sessions_client.delete(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["state"] == "recycled"
        # 幂等回收不重复落事件
        assert events_store.kinds("T-0412") == ["session.created", "session.recycled"]

    def test_recycle_unknown_session_404(self, sessions_client):
        r = sessions_client.delete(
            "/api/v1/execution/sessions/s-nope", headers=AUTH
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "session_not_found"

    def test_redispatch_after_recycle_reuses_branch(self, sessions_client):
        """一任务一分支：回收后重派，新会话复用同名分支。"""
        first = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        sessions_client.delete(
            f"/api/v1/execution/sessions/{first['id']}", headers=AUTH
        )
        second = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        assert second["id"] != first["id"]
        assert second["branch"] == first["branch"]

    def test_session_endpoints_require_auth(self, sessions_client):
        r = sessions_client.post(
            "/api/v1/execution/sessions", json=create_session_body()
        )
        assert r.status_code == 401
        r = sessions_client.delete("/api/v1/execution/sessions/s-1")
        assert r.status_code == 401


# ================================================================ W06-I04 · 会话控制（spawn/send/steer/kill）

def control_runner(**outputs) -> FakeOrcaRunner:
    """默认健康的 Orca CLI 假体：spawn 返回 session_id，其余子命令 rc=0。"""
    base: dict[str, exec_orca.RunnerResult] = {
        "spawn": exec_orca.RunnerResult(rc=0, stdout='{"session_id": "os-777"}', stderr="")
    }
    base.update(outputs)
    return FakeOrcaRunner(outputs=base)


def control_client(
    monkeypatch,
    tmp_repo: Path,
    sessions_store: FakeSessionStore,
    events_store: FakeEventStore,
    gateway: FakeTasksGateway,
    runner: FakeOrcaRunner,
    *,
    available: bool = True,
    which: str | None = "/bin/orca",
    artifact_root: Path | None = None,
    open_stores=None,
    langfuse_base: str | None = None,
) -> TestClient:
    """I04/I05 测试专用 App：probe 固定结果 + OrcaAdapter 挂假 CLI。"""
    async def status(*, force=False):
        return exec_orca.OrcaProbeResult(
            available=available,
            checked_at=utcnow(),
            cli_path="/bin/orca" if available else None,
            error=None if available else "未找到 Orca CLI：PATH 中无 `orca`，无法派 Orca 类任务",
        )

    monkeypatch.setattr(exec_service, "get_orca_status", status)
    app = create_app()
    app.dependency_overrides[exec_router.get_worker_store] = lambda: FakeWorkerStore()
    app.dependency_overrides[exec_router.get_event_store] = lambda: events_store
    app.dependency_overrides[exec_router.get_tasks_gateway] = lambda: gateway
    app.dependency_overrides[exec_router.get_session_store] = lambda: sessions_store
    app.dependency_overrides[exec_router.get_worktree_manager] = lambda: WorktreeManager(
        tmp_repo
    )
    app.dependency_overrides[exec_router.get_orca_adapter] = lambda: exec_orca.OrcaAdapter(
        runner, which=(lambda _: which)
    )
    if artifact_root is not None:
        app.dependency_overrides[exec_router.get_artifact_store] = lambda: (
            exec_harvest.LocalArtifactStore(artifact_root)
        )
    if open_stores is not None:
        app.dependency_overrides[exec_router.get_background_stores] = lambda: open_stores
    if langfuse_base is not None:
        app.dependency_overrides[exec_router.get_langfuse_base_url] = lambda: langfuse_base
    return TestClient(app)


def fake_open_stores(sessions_store, events_store):
    """后台 harvest 的 fake 装配：复用同一内存存储，方便断言最终状态。"""

    @asynccontextmanager
    async def _open():
        yield sessions_store, events_store

    return _open


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """轮询等待后台任务完成（TestClient 的 portal 在 with 内持续运行）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def spawned_session(client: TestClient, **overrides) -> dict[str, Any]:
    """建一个 runtime=orca 会话并 spawn 到 running；返回 spawn 后的视图。"""
    created = client.post(
        "/api/v1/execution/sessions",
        headers=AUTH,
        json=create_session_body(runtime="orca", **overrides),
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    r = client.post(
        f"/api/v1/execution/sessions/{session_id}/spawn",
        headers=AUTH,
        json={"prompt": "build the feature"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "running"
    return r.json()


class TestOrcaAdapter:
    async def test_spawn_parses_session_id(self):
        adapter = exec_orca.OrcaAdapter(control_runner(), which=lambda _: "/bin/orca")
        sid = await adapter.spawn(worktree="/wt", task_id="T-1", prompt="go")
        assert sid == "os-777"

    async def test_spawn_format_change_raises_explicitly(self):
        runner = control_runner(
            spawn=exec_orca.RunnerResult(rc=0, stdout="garbage", stderr="")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaFormatError, match="输出格式变了"):
            await adapter.spawn(worktree="/wt", task_id="T-1")

    async def test_spawn_command_failure(self):
        runner = control_runner(
            spawn=exec_orca.RunnerResult(rc=1, stdout="", stderr="boom")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaCommandError):
            await adapter.spawn(worktree="/wt", task_id="T-1")

    async def test_send_translates_exit_code_as_session_gone(self):
        runner = control_runner(
            send=exec_orca.RunnerResult(rc=4, stdout="", stderr="no such session")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaSessionGone):
            await adapter.send(session="os-777", prompt="hi")

    async def test_send_translates_error_payload_as_session_gone(self):
        runner = control_runner(
            send=exec_orca.RunnerResult(
                rc=1, stdout='{"error": "session_not_found"}', stderr=""
            )
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaSessionGone):
            await adapter.send(session="os-777", prompt="hi")

    async def test_send_generic_failure_is_command_error(self):
        runner = control_runner(
            send=exec_orca.RunnerResult(rc=1, stdout="", stderr="quota exceeded")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaCommandError, match="quota exceeded"):
            await adapter.send(session="os-777", prompt="hi")

    async def test_kill_passes_mode(self):
        runner = control_runner()
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        await adapter.kill(session="os-777", mode="hard")
        assert ["hard" in a for a in runner.calls].count(True) == 1

    async def test_missing_cli_raises(self):
        adapter = exec_orca.OrcaAdapter(control_runner(), which=lambda _: None)
        with pytest.raises(exec_orca.OrcaError, match="未找到 Orca CLI"):
            await adapter.spawn(worktree="/wt", task_id="T-1")


class TestSessionControl:
    def test_spawn_marks_running_and_registers_orca_session(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner
        )
        created = c.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(runtime="orca"),
        ).json()
        r = c.post(
            f"/api/v1/execution/sessions/{created['id']}/spawn",
            headers=AUTH,
            json={"prompt": "build it", "snapshot": "snap-1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "running"
        assert body["orca_session"] == "os-777"
        assert body["spawned_at"]
        # spawn 命令携带 worktree 与任务上下文
        spawn_call = next(a for a in runner.calls if "spawn" in a)
        assert created["worktree_path"] in spawn_call
        assert created["task_id"] in spawn_call
        assert "snap-1" in spawn_call
        assert events_store.kinds("T-0412") == ["session.created", "session.spawned"]

    def test_spawn_requires_orca_runtime(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner
        )
        created = c.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(),  # runtime 缺省 generic
        ).json()
        r = c.post(
            f"/api/v1/execution/sessions/{created['id']}/spawn",
            headers=AUTH,
            json={},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "session_not_runnable"
        assert runner.calls == []  # 前置条件不满足时绝不碰 CLI

    def test_spawn_blocked_when_probe_unavailable(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        """I02 验收延续：探测失败 → 拒绝派 Orca 任务，不猜。"""
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway,
            runner, available=False,
        )
        created = c.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(runtime="orca"),
        ).json()
        r = c.post(
            f"/api/v1/execution/sessions/{created['id']}/spawn", headers=AUTH, json={}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "orca_unavailable"
        assert runner.calls == []

    def test_spawn_missing_cli_between_probe_and_spawn(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        """TOCTOU：probe 说可用但 CLI 消失 → 明确 409，不 500。"""
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner,
            which=None,
        )
        created = c.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(runtime="orca"),
        ).json()
        r = c.post(
            f"/api/v1/execution/sessions/{created['id']}/spawn", headers=AUTH, json={}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "orca_unavailable"

    def test_spawn_rejects_non_created_state(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        spawned_session(c)
        session = next(iter(sessions_store.rows.values()))
        r = c.post(
            f"/api/v1/execution/sessions/{session.id}/spawn", headers=AUTH, json={}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "session_not_runnable"

    def test_send_appends_message_event(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        session = spawned_session(c)
        r = c.post(
            f"/api/v1/execution/sessions/{session['id']}/send",
            headers=AUTH,
            json={"prompt": "run the tests", "files": ["backend/tests/"]},
        )
        assert r.status_code == 200
        assert r.json() == {
            "session_id": session["id"],
            "task_id": "T-0412",
            "state": "running",
            "accepted": True,
        }
        assert events_store.kinds("T-0412") == [
            "session.created", "session.spawned", "session.message",
        ]

    def test_steer_appends_steer_event(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        session = spawned_session(c)
        r = c.post(
            f"/api/v1/execution/sessions/{session['id']}/steer",
            headers=AUTH,
            json={"message": "注意预算上限，收敛范围"},
        )
        assert r.status_code == 200
        assert events_store.kinds("T-0412")[-1] == "session.steer"

    def test_send_on_dead_session_marks_task_failed_and_releases_lease(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        """I04 验收 ④：会话崩了 → 任务回 failed（可重派）且租约释放。"""
        runner = control_runner(
            send=exec_orca.RunnerResult(rc=4, stdout="", stderr="no such session")
        )
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner
        )
        session = spawned_session(c)
        r = c.post(
            f"/api/v1/execution/sessions/{session['id']}/send",
            headers=AUTH,
            json={"prompt": "hi"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "session_crashed"
        # 任务侧：failed + retryable（可重派），租约已释放
        assert gateway.failed[0]["error_type"] == "session_crashed"
        assert gateway.failed[0]["retryable"] is True
        assert (session["task_id"], session["id"]) in gateway.released
        # 会话侧：终态 + 原因 + 事件
        assert sessions_store.rows[session["id"]].state == "failed"
        assert "会话" in (sessions_store.rows[session["id"]].error or "")
        assert events_store.kinds("T-0412")[-1] == "session.failed"
        # 死会话不再接收指令
        r2 = c.post(
            f"/api/v1/execution/sessions/{session['id']}/send",
            headers=AUTH,
            json={"prompt": "again"},
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "session_not_runnable"

    def test_kill_releases_lease_and_marks_killed(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner
        )
        session = spawned_session(c)
        r = c.post(
            f"/api/v1/execution/sessions/{session['id']}/kill", headers=AUTH, json={}
        )
        assert r.status_code == 200
        assert r.json()["state"] == "killed"
        assert (session["task_id"], session["id"]) in gateway.released
        kill_call = next(a for a in runner.calls if "kill" in a)
        assert "graceful" in kill_call  # 缺省优雅停：先 snapshot 再杀
        assert events_store.kinds("T-0412")[-1] == "session.killed"

    def test_kill_is_idempotent(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        session = spawned_session(c)
        first = c.post(
            f"/api/v1/execution/sessions/{session['id']}/kill", headers=AUTH, json={}
        )
        second = c.post(
            f"/api/v1/execution/sessions/{session['id']}/kill", headers=AUTH, json={}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["state"] == "killed"
        assert len(gateway.released) == 1  # 租约只释放一次

    def test_kill_never_spawned_session_skips_cli(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        runner = control_runner()
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner
        )
        created = c.post(
            "/api/v1/execution/sessions",
            headers=AUTH,
            json=create_session_body(runtime="orca"),
        ).json()
        r = c.post(
            f"/api/v1/execution/sessions/{created['id']}/kill", headers=AUTH, json={}
        )
        assert r.status_code == 200
        assert r.json()["state"] == "killed"
        assert runner.calls == []  # 没有 orca_session 就没有 CLI 调用
        assert (created["task_id"], created["id"]) in gateway.released

    def test_spawn_keeps_lease_alive_until_killed(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        """I04 验收 ②：会话即租约——存活期间自动续约，kill 即停。"""
        monkeypatch.setattr(exec_service, "LEASE_RENEW_SECONDS", 0.02)
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        with c:
            session = spawned_session(c)
            time.sleep(0.3)  # 主线程让路，portal 线程跑续约 keeper
            renews = [(tid, worker) for tid, worker in gateway.renews]
            assert len(renews) >= 2
            assert all(tid == session["task_id"] for tid, _ in renews)
            assert all(worker == session["id"] for _, worker in renews)

            r = c.post(
                f"/api/v1/execution/sessions/{session['id']}/kill",
                headers=AUTH,
                json={"mode": "hard"},
            )
            assert r.status_code == 200
            stopped = len(gateway.renews)
            time.sleep(0.1)
            assert len(gateway.renews) == stopped  # kill 后不再续约

    def test_recycle_refuses_running_session(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        session = spawned_session(c)
        r = c.delete(f"/api/v1/execution/sessions/{session['id']}", headers=AUTH)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "session_not_runnable"
        # kill 之后即可回收
        c.post(f"/api/v1/execution/sessions/{session['id']}/kill", headers=AUTH, json={})
        r2 = c.delete(f"/api/v1/execution/sessions/{session['id']}", headers=AUTH)
        assert r2.status_code == 200
        assert r2.json()["state"] == "recycled"

    def test_control_endpoints_require_auth(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        assert (
            c.post("/api/v1/execution/sessions/s-1/spawn", json={}).status_code == 401
        )
        assert (
            c.post("/api/v1/execution/sessions/s-1/send", json={"prompt": "x"}).status_code
            == 401
        )
        assert (
            c.post("/api/v1/execution/sessions/s-1/steer", json={"message": "x"}).status_code
            == 401
        )
        assert c.post("/api/v1/execution/sessions/s-1/kill", json={}).status_code == 401


# ================================================================ W06-I05 · harvest 归集

class TestSummarizeNumstat:
    def test_happy_path(self):
        out = "3\t1\tbackend/app/x.py\n12\t0\tbackend/tests/test_x.py\n"
        s = exec_harvest.summarize_numstat(out)
        assert s.files_changed == 2
        assert s.insertions == 15
        assert s.deletions == 1
        assert s.dirs == ("backend",)
        assert s.files == ("backend/app/x.py", "backend/tests/test_x.py")

    def test_binary_and_rename(self):
        out = "-\t-\tassets/logo.png\n0\t5\tmain.py => src/main.py\n"
        s = exec_harvest.summarize_numstat(out)
        assert s.files_changed == 2
        assert s.binary == 1
        assert s.insertions == 0
        assert s.deletions == 5
        assert "src/main.py" in s.files
        assert s.dirs == ("assets", "src")

    def test_truncates_file_list(self):
        out = "\n".join(f"1\t0\tdir/f{i}.py" for i in range(80))
        s = exec_harvest.summarize_numstat(out)
        assert s.files_changed == 80
        assert len(s.files) == 50  # 行记录防撑爆：只留前 50 个


class TestLocalArtifactStore:
    async def test_put_text_writes_file_and_returns_ref(self, tmp_path):
        store = exec_harvest.LocalArtifactStore(tmp_path / "art")
        ref = await store.put_text(
            "transcripts/T-0412/s-1.jsonl", '{"role": "user"}\n'
        )
        assert ref.startswith("file://")
        path = Path(ref.removeprefix("file://"))
        assert path.read_text(encoding="utf-8") == '{"role": "user"}\n'


class TestOrcaAdapterHarvest:
    async def test_harvest_parses_payload(self):
        payload = {"transcript_path": "/tmp/t.jsonl", "cost": {"by_model": {"claude": 0.31}}}
        runner = control_runner(
            harvest=exec_orca.RunnerResult(rc=0, stdout=json.dumps(payload), stderr="")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        assert await adapter.harvest(session="os-777") == payload

    async def test_harvest_format_change_raises_explicitly(self):
        runner = control_runner(
            harvest=exec_orca.RunnerResult(rc=0, stdout="not json", stderr="")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaFormatError, match="输出格式变了"):
            await adapter.harvest(session="os-777")

    async def test_harvest_session_gone(self):
        runner = control_runner(
            harvest=exec_orca.RunnerResult(rc=4, stdout="", stderr="no such session")
        )
        adapter = exec_orca.OrcaAdapter(runner, which=lambda _: "/bin/orca")
        with pytest.raises(exec_orca.OrcaSessionGone):
            await adapter.harvest(session="os-777")


class TestSessionHarvest:
    def test_harvest_collects_diff_transcript_and_cost(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway, tmp_path
    ):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"role": "user"}\n', encoding="utf-8")
        runner = control_runner(
            harvest=exec_orca.RunnerResult(
                rc=0,
                stdout=json.dumps(
                    {
                        "transcript_path": str(transcript),
                        "cost": {"by_model": {"claude": 0.31, "haiku": 0.02}},
                    }
                ),
                stderr="",
            )
        )
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner,
            artifact_root=tmp_path / "artifacts",
            open_stores=fake_open_stores(sessions_store, events_store),
        )
        with c:
            session = spawned_session(c)
            # Agent 的工作：往分支上提交一个文件（diff 即产物）
            wt = Path(session["worktree_path"])
            (wt / "backend" / "app").mkdir(parents=True)
            (wt / "backend" / "app" / "feature.py").write_text("print('v1')\n", encoding="utf-8")
            git_commit(wt, "feat")

            r = c.post(
                f"/api/v1/execution/sessions/{session['id']}/harvest", headers=AUTH
            )
            assert r.status_code == 202
            assert r.json()["state"] == "harvesting"

            sid = session["id"]
            assert wait_until(
                lambda: sessions_store.rows[sid].state == "harvested"
            ), f"harvest 未完成，state={sessions_store.rows[sid].state}"

            row = sessions_store.rows[sid]
            # 验收 ①：diff 摘要入库
            assert row.diff_summary["files_changed"] == 1
            assert row.diff_summary["insertions"] == 1
            assert row.diff_summary["dirs"] == ["backend"]
            # 验收 ②：库里只存 transcript 引用，内容在对象存储
            assert row.transcript_url and row.transcript_url.startswith("file://")
            stored = Path(row.transcript_url.removeprefix("file://"))
            assert stored.read_text(encoding="utf-8") == '{"role": "user"}\n'
            assert "transcripts/T-0412" in row.transcript_url
            # 验收 ③：成本按模型分项写 task_events（供 W07 归集）
            kinds = events_store.kinds("T-0412")
            assert kinds == [
                "session.created", "session.spawned",
                "session.harvesting", "session.harvested", "cost", "cost",
            ]
            models = [
                e.payload["model"]
                for e in events_store.rows
                if e.task_id == "T-0412" and e.kind == "cost"
            ]
            assert models == ["claude", "haiku"]  # 按模型名排序分项

    def test_harvest_is_async_and_does_not_block_request(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway, tmp_path
    ):
        """验收 ④：大 diff 不阻塞主流程——202 立即返回，收集后台完成。"""
        runner = control_runner(delay=0.4)
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner,
            artifact_root=tmp_path / "artifacts",
            open_stores=fake_open_stores(sessions_store, events_store),
        )
        with c:
            session = spawned_session(c)
            wt = Path(session["worktree_path"])
            (wt / "feat.py").write_text("x = 1\n", encoding="utf-8")
            git_commit(wt, "feat")

            t0 = time.time()
            r = c.post(
                f"/api/v1/execution/sessions/{session['id']}/harvest", headers=AUTH
            )
            elapsed = time.time() - t0
            assert r.status_code == 202
            assert elapsed < 0.3  # 请求不等 harvest（runner 睡 0.4s）

            sid = session["id"]
            assert wait_until(
                lambda: sessions_store.rows[sid].state == "harvested"
            ), "后台 harvest 未完成"
            assert sessions_store.rows[sid].diff_summary["files_changed"] == 1

    def test_harvest_without_artifacts_fails_the_task(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway, tmp_path
    ):
        """无产物 → 判定失败：任务 failed（不重试）+ 租约释放。"""
        runner = control_runner()  # harvest 输出空 → 无 transcript
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner,
            artifact_root=tmp_path / "artifacts",
            open_stores=fake_open_stores(sessions_store, events_store),
        )
        with c:
            session = spawned_session(c)  # 无任何提交 → diff 为空
            r = c.post(
                f"/api/v1/execution/sessions/{session['id']}/harvest", headers=AUTH
            )
            assert r.status_code == 202

            sid = session["id"]
            assert wait_until(
                lambda: sessions_store.rows[sid].state == "failed"
            ), "无产物判定未落库"

            assert gateway.failed[0]["error_type"] == "no_artifacts"
            assert gateway.failed[0]["retryable"] is False
            assert (session["task_id"], sid) in gateway.released
            assert sessions_store.rows[sid].error and "无产物" in sessions_store.rows[sid].error
            assert events_store.kinds("T-0412")[-1] == "session.failed"

    def test_harvest_rejects_bad_states(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway, tmp_path
    ):
        runner = control_runner(delay=0.4)
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, runner,
            artifact_root=tmp_path / "artifacts",
            open_stores=fake_open_stores(sessions_store, events_store),
        )
        with c:
            # created（未 spawn）：没有可收集的运行痕迹
            created = c.post(
                "/api/v1/execution/sessions",
                headers=AUTH,
                json=create_session_body(runtime="orca"),
            ).json()
            r = c.post(
                f"/api/v1/execution/sessions/{created['id']}/harvest", headers=AUTH
            )
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "session_not_runnable"

            # 收集中重复 harvest → 拒绝（换 slug 避开上一条的占用）
            session = spawned_session(c, slug="state-check")
            wt = Path(session["worktree_path"])
            (wt / "feat.py").write_text("x = 1\n", encoding="utf-8")
            git_commit(wt, "feat")
            r1 = c.post(
                f"/api/v1/execution/sessions/{session['id']}/harvest", headers=AUTH
            )
            r2 = c.post(
                f"/api/v1/execution/sessions/{session['id']}/harvest", headers=AUTH
            )
            assert r1.status_code == 202
            assert r2.status_code == 409

    def test_harvest_requires_auth(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway, tmp_path
    ):
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway, control_runner()
        )
        assert (
            c.post("/api/v1/execution/sessions/s-1/harvest").status_code == 401
        )


# ================================================================ W06-I06 · 会话视图数据接口

class TestSessionViewApi:
    def test_list_sessions_with_filters(self, sessions_client):
        def create(slug: str, **overrides) -> dict:
            r = sessions_client.post(
                "/api/v1/execution/sessions",
                headers=AUTH,
                json=create_session_body(slug=slug, **overrides),
            )
            assert r.status_code == 201
            return r.json()

        create("a", runtime="orca", device_id="d1", objective_id="O-1")
        create("b", runtime="generic", device_id="d2", objective_id="O-1")
        create("c", runtime="orca", device_id="d1", objective_id="O-2")

        r = sessions_client.get("/api/v1/execution/sessions", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 3
        assert body["has_more"] is False
        assert body["limit"] == 50 and body["offset"] == 0
        # 新创建的在最前（created_at 倒序）
        times = [i["created_at"] for i in body["items"]]
        assert times == sorted(times, reverse=True)

        def slugs(params: dict) -> set[str]:
            rr = sessions_client.get(
                "/api/v1/execution/sessions", headers=AUTH, params=params
            )
            assert rr.status_code == 200
            return {i["slug"] for i in rr.json()["items"]}

        assert slugs({"runtime": "orca"}) == {"a", "c"}
        assert slugs({"device_id": "d1"}) == {"a", "c"}
        assert slugs({"objective_id": "O-2"}) == {"c"}
        assert slugs({"state": "created"}) == {"a", "b", "c"}
        assert slugs({"device_id": "d1", "objective_id": "O-1"}) == {"a"}

    def test_list_sessions_pagination(self, sessions_client):
        for slug in ("p1", "p2", "p3"):
            r = sessions_client.post(
                "/api/v1/execution/sessions",
                headers=AUTH,
                json=create_session_body(slug=slug),
            )
            assert r.status_code == 201

        page1 = sessions_client.get(
            "/api/v1/execution/sessions", headers=AUTH, params={"limit": 2}
        ).json()
        page2 = sessions_client.get(
            "/api/v1/execution/sessions",
            headers=AUTH,
            params={"limit": 2, "offset": 2},
        ).json()
        assert page1["has_more"] is True
        assert len(page1["items"]) == 2
        assert page2["has_more"] is False
        assert len(page2["items"]) == 1
        ids1 = {i["id"] for i in page1["items"]}
        ids2 = {i["id"] for i in page2["items"]}
        assert not (ids1 & ids2)  # 两页不重叠

    def test_detail_returns_langfuse_url_and_events(
        self, monkeypatch, tmp_repo, sessions_store, events_store, gateway
    ):
        """验收 ①：只给链接与前 20 条事件，不代理 Langfuse 内容。"""
        c = control_client(
            monkeypatch, tmp_repo, sessions_store, events_store, gateway,
            control_runner(), langfuse_base="https://lf.example.com/",
        )
        session = spawned_session(c)
        c.post(
            f"/api/v1/execution/sessions/{session['id']}/send",
            headers=AUTH,
            json={"prompt": "step one"},
        )
        c.post(
            f"/api/v1/execution/sessions/{session['id']}/send",
            headers=AUTH,
            json={"prompt": "step two"},
        )
        r = c.get(f"/api/v1/execution/sessions/{session['id']}", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # 链接按 task_id 拼，前端 iframe 嵌入
        assert body["langfuse_url"] == "https://lf.example.com/trace/T-0412"
        assert [e["kind"] for e in body["events"]] == [
            "session.created", "session.spawned", "session.message", "session.message",
        ]
        assert body["events"][0]["source"] == "service"
        assert body["events"][0]["session_id"] == session["id"]
        assert body["events"][2]["payload"] == {"chars": 8, "files": []}

    def test_detail_without_langfuse_base_returns_null_url(self, sessions_client):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        r = sessions_client.get(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        assert r.status_code == 200
        body = r.json()
        assert body["langfuse_url"] is None  # 未配置 base_url 时不给假链接
        assert [e["kind"] for e in body["events"]] == ["session.created"]

    def test_detail_caps_events_at_20(
        self, sessions_client, sessions_store, events_store
    ):
        created = sessions_client.post(
            "/api/v1/execution/sessions", headers=AUTH, json=create_session_body()
        ).json()
        for i in range(25):
            asyncio.run(
                events_store.append(
                    task_id="T-0412",
                    kind="note",
                    payload={"i": i},
                    session_id=created["id"],
                )
            )
        r = sessions_client.get(
            f"/api/v1/execution/sessions/{created['id']}", headers=AUTH
        )
        events = r.json()["events"]
        assert len(events) == 20  # 只取前 20 条（id 升序，最早的在前）
        assert events[0]["kind"] == "session.created"
        assert events[-1]["payload"] == {"i": 18}

    def test_detail_unknown_session_404(self, sessions_client):
        r = sessions_client.get(
            "/api/v1/execution/sessions/s-nope", headers=AUTH
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "session_not_found"

    def test_view_endpoints_require_auth(self, sessions_client):
        assert sessions_client.get("/api/v1/execution/sessions").status_code == 401
        assert (
            sessions_client.get("/api/v1/execution/sessions/s-1").status_code == 401
        )


# ================================================================ 启动清扫（服务重启恢复）

class TestStartupSweep:
    """fail_stale_running_sessions：上一进程遗留的 running 会话重启后标记 failed。"""

    @staticmethod
    def _row(sid: str, state: str) -> SessionRow:
        return SessionRow(
            id=sid,
            task_id=f"t-{sid}",
            issue="W06",
            slug=sid,
            branch=f"win/{sid}",
            worktree_path=f"/tmp/wt-{sid}",
            state=state,
        )

    async def test_running_marked_failed_with_event(self):
        store = FakeSessionStore()
        store.rows["s-live"] = self._row("s-live", "running")
        store.rows["s-done"] = self._row("s-done", "recycled")
        events = FakeEventStore()

        n = await exec_service.fail_stale_running_sessions(store, events)

        assert n == 1
        assert store.rows["s-live"].state == "failed"
        assert store.rows["s-live"].ended_at is not None
        assert store.rows["s-done"].state == "recycled"  # 终态不动
        kinds = [e.kind for e in events.rows if e.session_id == "s-live"]
        assert kinds == ["session.failed"]

    async def test_no_running_sessions_is_noop(self):
        store = FakeSessionStore()
        store.rows["s-x"] = self._row("s-x", "failed")
        events = FakeEventStore()
        assert await exec_service.fail_stale_running_sessions(store, events) == 0
        assert events.rows == []
