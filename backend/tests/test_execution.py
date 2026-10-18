"""W06 执行面测试。

原则（见 plan/W06.md 各 issue 验收）：
- 不依赖真实 Postgres/Redis/Orca：存储用内存 fake，tasks 用 FakeTasksGateway，
  Orca 探测 mock subprocess，worktree 用临时 git 仓库；
- 端到端语义（claim→complete 的 fake worker）必须在 HTTP 层跑通。
"""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_token
from app.main import create_app
from app.modules.execution import orca as exec_orca
from app.modules.execution import protocol as proto
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
