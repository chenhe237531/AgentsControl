"""W06 执行面测试。

原则（见 plan/W06.md 各 issue 验收）：
- 不依赖真实 Postgres/Redis/Orca：存储用内存 fake，tasks 用 FakeTasksGateway，
  Orca 探测 mock subprocess，worktree 用临时 git 仓库；
- 端到端语义（claim→complete 的 fake worker）必须在 HTTP 层跑通。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_token
from app.main import create_app
from app.modules.execution import orca as exec_orca
from app.modules.execution import protocol as proto
from app.modules.execution.protocol import LeasedTask
from app.modules.execution.repository import EventRecord, WorkerInfo

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

    async def lease(self, *, capabilities: list[str], slots: int, machine: str | None):
        self.lease_calls.append(
            {"capabilities": capabilities, "slots": slots, "machine": machine}
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
    def test_claim_reports_502_when_tasks_module_missing(self, workers_store, events_store):
        """W03 未交付时的行为：明确 502 tasks_unavailable，绝不降级猜测。"""
        app = create_app()
        app.dependency_overrides[exec_router.get_worker_store] = lambda: workers_store
        app.dependency_overrides[exec_router.get_event_store] = lambda: events_store
        app.dependency_overrides[exec_router.get_tasks_gateway] = (
            lambda: proto.TasksServiceGateway()
        )
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/execution/workers/claim",
                headers=AUTH,
                json={"capabilities": ["orca"], "concurrency_slots": 1},
            )
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "tasks_unavailable"


class TestTasksServiceGatewayContract:
    """网关对 tasks.service 返回形态的防御性契约（W03 交付前锁定口径）。"""

    @staticmethod
    def _install_stub(monkeypatch, lease_return: Any) -> None:
        import sys
        import types

        mod = types.ModuleType("app.modules.tasks.service")

        async def lease(*, capabilities, slots, machine):
            return lease_return

        async def renew_lease(*, task_id, worker):
            return utcnow()

        async def complete(*, task_id, worker, result):
            return None

        async def fail(*, task_id, worker, error_type, retryable, context=None):
            return None

        mod.lease = lease
        mod.renew_lease = renew_lease
        mod.complete = complete
        mod.fail = fail
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", mod)

    @pytest.mark.asyncio
    async def test_lease_accepts_dict_shape(self, monkeypatch):
        self._install_stub(
            monkeypatch, {"task_id": "T-1", "payload": {"k": 1}, "lease_expires_at": utcnow()}
        )
        gw = proto.TasksServiceGateway()
        leased = await gw.lease(capabilities=[], slots=1, machine=None)
        assert leased is not None
        assert leased.task_id == "T-1"

    @pytest.mark.asyncio
    async def test_lease_rejects_garbage(self, monkeypatch):
        self._install_stub(monkeypatch, {"payload": {}})  # 缺 task_id → 拒绝
        gw = proto.TasksServiceGateway()
        with pytest.raises(proto.TasksUnavailableError):
            await gw.lease(capabilities=[], slots=1, machine=None)

    @pytest.mark.asyncio
    async def test_missing_service_functions_raise_unavailable(self, monkeypatch):
        import sys
        import types

        empty = types.ModuleType("app.modules.tasks.service")
        monkeypatch.setitem(sys.modules, "app.modules.tasks.service", empty)
        gw = proto.TasksServiceGateway()
        with pytest.raises(proto.TasksUnavailableError):
            await gw.lease(capabilities=[], slots=1, machine=None)


# ================================================================ W06-I02 · Orca probe

@dataclass
class FakeOrcaRunner:
    """按子命令返回预设输出的假 CLI。"""

    outputs: dict[str, exec_orca.RunnerResult]
    calls: list[list[str]] = field(default_factory=list)

    async def run(self, args: list[str], *, timeout: float = 5.0) -> exec_orca.RunnerResult:
        self.calls.append(args)
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
