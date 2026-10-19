"""W03 · 任务队列与租约测试。

分两层：

1. **纯函数层**（无需数据库，CI backend job 会跑）
   状态机矩阵、能力匹配、预算阀门、调度退避、以及「状态只在 repository 里写」的静态守护。
2. **真实 Postgres 层**（需要 `TEST_DATABASE_URL`，没有则 skip）
   `SKIP LOCKED`、`UPDATE ... WHERE leased_until < now()` 这类行为只有真库能验证，
   mock 出来的是"我以为的 Postgres"，不是 Postgres。

为什么 DB 测试要 skip 而不是用 SQLite：
SQLite 没有 `FOR UPDATE SKIP LOCKED`、没有 JSONB、没有 `gen_random_uuid()`，
用它跑队列测试只会得到虚假的绿灯。CI 目前只在 migrations job 里起了 PG，
`backend` job 没有——这是需要走 x-shared-registry 补的 CI 配置（已在 PR 描述里记录）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.modules.tasks as tasks_pkg
from app.core.db import Base, get_db
from app.core.errors import ConflictError
from app.core.security import create_token
from app.main import create_app
from app.modules.tasks import machine, matcher, repository, scheduler, service, valve
from app.modules.tasks.machine import TaskEvent, TaskState
from app.modules.tasks.schemas import ClaimIn, LeaseIn, RequeueIn, TaskCreate
from app.modules.tasks.settings import TaskSettings

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "").strip()
requires_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="需要 TEST_DATABASE_URL 指向真实 Postgres；CI backend job 未提供 DB 服务",
)


# --------------------------------------------------------------------------- #
# 纯函数层
# --------------------------------------------------------------------------- #
def test_state_machine_matrix_is_exhaustive() -> None:
    """穷举状态 × 事件：要么给出唯一目标状态，要么抛 409，不存在第三种结果。"""
    for state in machine.all_states():
        for event in machine.all_events():
            target = machine.TRANSITIONS[state].get(event)
            if target is None:
                with pytest.raises(ConflictError) as exc:
                    machine.next_state(state, event)
                assert exc.value.status_code == 409
                assert exc.value.code == "illegal_transition"
                assert exc.value.details["state"] == state.value
                assert exc.value.details["event"] == event.value
            else:
                assert machine.next_state(state, event) == target


def test_every_state_declares_its_own_row() -> None:
    """矩阵必须覆盖全部状态，新增状态却忘了加行会在这里炸。"""
    assert set(machine.TRANSITIONS) == set(machine.all_states())
    assert len(machine.matrix()) == len(machine.all_states()) * len(machine.all_events())


def test_terminal_states_accept_nothing() -> None:
    for state in (TaskState.DONE, TaskState.CANCELLED):
        assert machine.is_terminal(state)
        assert machine.allowed_events(state) == ()


def test_reclaim_exhaustion_goes_dead_not_dropped() -> None:
    """连续回收耗尽后进死信，绝不静默丢弃（产品红线）。"""
    assert machine.next_state(TaskState.LEASED, TaskEvent.RECLAIM_EXHAUSTED) is TaskState.DEAD
    assert machine.next_state(TaskState.RUNNING, TaskEvent.RECLAIM_EXHAUSTED) is TaskState.DEAD


def test_heartbeat_is_self_loop_on_both_leased_states() -> None:
    assert machine.next_state(TaskState.LEASED, TaskEvent.HEARTBEAT) is TaskState.LEASED
    assert machine.next_state(TaskState.RUNNING, TaskEvent.HEARTBEAT) is TaskState.RUNNING


def test_dead_requires_manual_requeue() -> None:
    """死信没有 retry 事件，只有 requeue（人工）。"""
    assert not machine.can(TaskState.DEAD, TaskEvent.RETRY)
    assert machine.next_state(TaskState.DEAD, TaskEvent.REQUEUE) is TaskState.QUEUED


def test_settings_rejects_lease_shorter_than_two_heartbeats() -> None:
    with pytest.raises(ValueError) as exc:
        TaskSettings(lease_seconds=45, heartbeat_seconds=30)
    assert "TASK_LEASE_SECONDS" in str(exc.value)


def test_settings_defaults_are_sane() -> None:
    cfg = TaskSettings()
    assert cfg.lease_seconds >= cfg.heartbeat_seconds * 2
    assert cfg.clamp_lease(None) == cfg.lease_seconds
    assert cfg.clamp_lease(1) == cfg.lease_min_seconds
    assert cfg.clamp_lease(10**6) == cfg.lease_max_seconds


def test_capability_matrix_five_rows() -> None:
    """架构文档 §4.4 的五类设备 × 典型任务，逐行验证「不匹配就不派」。"""
    profiles = matcher.DEVICE_PROFILES
    cases = [
        # (设备, requires, 期望可派, 原因关键字)
        ("always_on_mac", {"gui": True}, True, ""),
        ("always_on_mac", {"always_on": True}, True, ""),
        ("remote_mac_mini", {"always_on": True}, True, ""),
        ("laptop", {"always_on": True}, False, "休眠"),
        ("laptop", {"gui": True}, True, ""),
        ("vps", {"gui": True}, False, "图形界面"),
        ("vps", {"orca": True}, False, "Orca"),
        ("vps", {"browser": True}, True, ""),
        ("vps", {"always_on": True}, True, ""),
        ("third_party", {"browser": True}, False, "浏览器"),
        ("third_party", {"always_on": True}, False, "休眠"),
    ]
    for device, requires, eligible, keyword in cases:
        result = matcher.evaluate(requires, profiles[device])
        assert result.eligible is eligible, (device, requires, result.reasons)
        if not eligible:
            assert any(keyword in reason for reason in result.reasons)


def test_matcher_reasons_are_display_ready() -> None:
    result = matcher.evaluate({"always_on": True}, matcher.DEVICE_PROFILES["laptop"])
    assert result.reasons == ("这台机器会休眠，不能承接常驻任务",)


def test_matcher_without_caps_rejects_capability_tasks() -> None:
    assert not matcher.is_eligible({"gui": True}, {})
    assert matcher.is_eligible({}, {})


def test_matcher_sql_conditions_cover_every_rule() -> None:
    fragments = matcher.requires_sql_conditions("t")
    assert len(fragments) == len(matcher.CAPABILITY_RULES)
    for rule in matcher.CAPABILITY_RULES:
        assert any(f"requires->>'{rule.requires_key}'" in f for f in fragments)


def _sql_equivalent(requires: dict[str, bool], caps: dict[str, bool]) -> bool:
    """按 SQL 谓词的写法复算一遍，用来证明两边规则没有漂移。"""
    return (
        (not requires.get("gui", False) or caps.get("has_gui", False))
        and (not requires.get("orca", False) or caps.get("has_orca", False))
        and (not requires.get("browser", False) or caps.get("has_browser", False))
        and (not requires.get("always_on", False) or not caps.get("can_sleep", False))
    )


def test_matcher_sql_matches_rules() -> None:
    task_shapes = [
        {},
        {"gui": True},
        {"orca": True},
        {"browser": True},
        {"always_on": True},
        {"gui": True, "always_on": True},
    ]
    for caps in matcher.DEVICE_PROFILES.values():
        for requires in task_shapes:
            assert matcher.is_eligible(requires, caps) == _sql_equivalent(requires, caps)


def test_valve_reserves_ten_percent() -> None:
    decision = valve.evaluate(cost_spent=90, budget_cap=100, reserve_ratio=0.10)
    assert not decision.allowed
    assert decision.available == Decimal("-0.0000") or decision.available <= 0
    assert "预算不足" in (decision.reason or "")


def test_valve_allows_unlimited_task() -> None:
    decision = valve.evaluate(cost_spent=10_000, budget_cap=None, reserve_ratio=0.10)
    assert decision.allowed
    assert decision.reason is None


def test_valve_zero_cap_holds() -> None:
    assert not valve.evaluate(cost_spent=0, budget_cap=0, reserve_ratio=0.10).allowed


def test_valve_projected_cost_counts() -> None:
    assert valve.evaluate(
        cost_spent=0, budget_cap=100, reserve_ratio=0.10, projected_cost=85
    ).allowed
    assert not valve.evaluate(
        cost_spent=0, budget_cap=100, reserve_ratio=0.10, projected_cost=91
    ).allowed


def test_valve_sql_predicate_matches_python() -> None:
    """claim 的 SQL 过滤条件必须与纯函数判定一致，否则会出现「能领到但派不出」。"""
    for cap, spent in [
        (None, Decimal("0")),
        (Decimal("0"), Decimal("0")),
        (Decimal("100"), Decimal("0")),
        (Decimal("100"), Decimal("89")),
        (Decimal("100"), Decimal("90")),
        (Decimal("10"), Decimal("9.5")),
    ]:
        python_allowed = valve.evaluate(
            cost_spent=spent, budget_cap=cap, reserve_ratio=0.10
        ).allowed
        sql_allowed = cap is None or spent + cap * Decimal("0.10") < cap
        assert python_allowed is sql_allowed, (cap, spent)


def test_backoff_is_exponential_and_capped() -> None:
    assert scheduler.backoff_seconds(1, base_seconds=30, max_seconds=3600) == 30
    assert scheduler.backoff_seconds(2, base_seconds=30, max_seconds=3600) == 60
    assert scheduler.backoff_seconds(3, base_seconds=30, max_seconds=3600) == 120
    assert scheduler.backoff_seconds(50, base_seconds=30, max_seconds=3600) == 3600


def test_retry_exhaustion() -> None:
    assert not scheduler.is_retry_exhausted(1, 3)
    assert not scheduler.is_retry_exhausted(3, 3)
    assert scheduler.is_retry_exhausted(4, 3)


def test_fairness_trigger_and_counter() -> None:
    assert not scheduler.should_force_low(4, 5)
    assert scheduler.should_force_low(5, 5)
    assert scheduler.next_consecutive_high(2, 0) == 3
    assert scheduler.next_consecutive_high(3, 8) == 0
    assert scheduler.next_consecutive_high(3, 5) == 3


def test_priority_bands() -> None:
    assert scheduler.band(0) == "high"
    assert scheduler.band(3) == "high"
    assert scheduler.band(4) == "normal"
    assert scheduler.band(6) == "normal"
    assert scheduler.band(7) == "low"
    assert scheduler.band(9) == "low"


def test_state_writes_are_confined_to_repository() -> None:
    """只有 repository 能写 state；service/router 出现 UPDATE tasks 就算越权。"""
    module_dir = Path(tasks_pkg.__file__).parent
    offenders = [
        path.name
        for path in module_dir.glob("*.py")
        if path.name not in {"repository.py", "models.py", "machine.py"}
        and ("UPDATE tasks" in path.read_text(encoding="utf-8") or "SET state" in path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --------------------------------------------------------------------------- #
# 真实 Postgres 层
# --------------------------------------------------------------------------- #
@pytest.fixture
async def engine():  # noqa: ANN201 - pytest fixture
    if not TEST_DB_URL:
        pytest.skip("缺少 TEST_DATABASE_URL")
    eng = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sm(engine):  # noqa: ANN001, ANN201
    return async_sessionmaker(engine, expire_on_commit=False)


async def _truncate(session) -> None:  # noqa: ANN001
    await session.execute(
        sa.text("TRUNCATE tasks, dead_letter, task_device_stats RESTART IDENTITY CASCADE")
    )
    await session.commit()


@pytest.fixture
async def db(sm):  # noqa: ANN001, ANN201
    async with sm() as session:
        await _truncate(session)
        yield session


@pytest.fixture
async def api(sm):  # noqa: ANN001, ANN201
    async with sm() as session:
        await _truncate(session)

    application = create_app()

    async def _override_db():
        async with sm() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_db] = _override_db
    token = create_token("owner-1", "access", extra={"role": "owner"})
    transport = ASGITransport(app=application)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


async def _create(**kwargs) -> TaskCreate:
    return TaskCreate(**{"title": "修复上传超时", **kwargs})


@requires_db
async def test_create_and_get_task(api) -> None:  # noqa: ANN001
    response = await api.post(
        "/api/v1/tasks",
        json={"title": "修复上传超时", "priority": 3, "requires": {"gui": True}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "queued"
    assert body["priority"] == 3
    assert body["requires"] == {"gui": True, "orca": False, "browser": False, "always_on": False}
    assert body["queued_overdue"] is False

    fetched = await api.get(f"/api/v1/tasks/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


@requires_db
async def test_list_tasks_filters_and_order(api) -> None:  # noqa: ANN001
    await api.post("/api/v1/tasks", json={"title": "低优", "priority": 8})
    await api.post("/api/v1/tasks", json={"title": "高优", "priority": 1})

    listing = await api.get("/api/v1/tasks", params={"state": "queued"})
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["count"] == 2
    assert [item["title"] for item in payload["items"]] == ["高优", "低优"]

    filtered = await api.get("/api/v1/tasks", params={"priority_max": 3})
    assert [item["title"] for item in filtered.json()["items"]] == ["高优"]


@requires_db
async def test_create_over_budget_goes_held(api) -> None:  # noqa: ANN001
    response = await api.post(
        "/api/v1/tasks", json={"title": "越预算", "budget_cap": "0"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "held"
    assert "预算不足" in (body["hold_reason"] or "")


@requires_db
async def test_illegal_transition_returns_409(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "排队中"})).json()
    response = await api.post(
        f"/api/v1/tasks/{created['id']}/complete",
        json={"lease_token": str(uuid.uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "illegal_transition"
    assert "allowed_events" in response.json()["error"]["details"]


@requires_db
async def test_lease_heartbeat_complete(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "跑一次"})).json()
    leased = await api.post(
        f"/api/v1/tasks/{created['id']}/lease",
        json={"device_id": "mac-1", "caps": {}},
    )
    assert leased.status_code == 200
    lease_body = leased.json()
    token = lease_body["lease_token"]
    assert lease_body["task"]["state"] == "leased"
    assert lease_body["heartbeat_seconds"] == 30

    beat = await api.post(
        f"/api/v1/tasks/{created['id']}/heartbeat", json={"lease_token": token}
    )
    assert beat.status_code == 200
    assert beat.json()["leased_until"] > leased.json()["leased_until"]

    done = await api.post(
        f"/api/v1/tasks/{created['id']}/complete",
        json={"lease_token": token, "result": {"pr": 42}},
    )
    assert done.status_code == 200
    assert done.json()["state"] == "done"
    assert done.json()["result"] == {"pr": 42}

    replay = await api.post(
        f"/api/v1/tasks/{created['id']}/heartbeat", json={"lease_token": token}
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "lease_lost"


@requires_db
async def test_second_lease_is_conflict(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "抢活"})).json()
    first = await api.post(
        f"/api/v1/tasks/{created['id']}/lease", json={"device_id": "mac-1", "caps": {}}
    )
    assert first.status_code == 200
    second = await api.post(
        f"/api/v1/tasks/{created['id']}/lease", json={"device_id": "mac-2", "caps": {}}
    )
    assert second.status_code == 409
    # 已被领走 → 状态机先拦（leased 不接受 lease 事件）
    assert second.json()["error"]["code"] == "illegal_transition"


@requires_db
async def test_lease_rejects_capability_mismatch(api) -> None:  # noqa: ANN001
    created = (
        await api.post("/api/v1/tasks", json={"title": "桌面自动化", "requires": {"gui": True}})
    ).json()
    response = await api.post(
        f"/api/v1/tasks/{created['id']}/lease",
        json={"device_id": "vps-1", "caps": {"has_gui": False}},
    )
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "capability_mismatch"
    assert any("图形界面" in reason for reason in body["details"]["reasons"])


@requires_db
async def test_claim_respects_capabilities(api) -> None:  # noqa: ANN001
    await api.post("/api/v1/tasks", json={"title": "需要 GUI", "requires": {"gui": True}})

    blind = await api.post(
        "/api/v1/tasks/claim", json={"device_id": "vps-1", "caps": {"has_gui": False}}
    )
    assert blind.status_code == 204

    capable = await api.post(
        "/api/v1/tasks/claim", json={"device_id": "mac-1", "caps": {"has_gui": True}}
    )
    assert capable.status_code == 200
    assert capable.json()["task"]["title"] == "需要 GUI"


@requires_db
async def test_claim_skips_budget_exhausted(api, db) -> None:  # noqa: ANN001
    created = (
        await api.post("/api/v1/tasks", json={"title": "没钱了", "budget_cap": "10"})
    ).json()
    # 模拟 W07 的记账结果（本窗口不写 cost_spent 的业务路径）
    await db.execute(
        sa.text("UPDATE tasks SET cost_spent = 9.5 WHERE id = :id"),
        {"id": uuid.UUID(created["id"])},
    )
    await db.commit()

    response = await api.post("/api/v1/tasks/claim", json={"device_id": "mac-1", "caps": {}})
    assert response.status_code == 204


@requires_db
async def test_concurrent_lease_has_single_winner(sm, db) -> None:  # noqa: ANN001
    task = await service.create_task(db, await _create())

    async def try_lease(device_id: str):  # noqa: ANN202
        async with sm() as session:
            leased = await repository.lease_task(
                session, task.id, device_id=device_id, lease_seconds=90
            )
            await session.commit()
            return leased

    results = await asyncio.gather(try_lease("mac-1"), try_lease("mac-2"))
    winners = [task_ for task_ in results if task_ is not None]
    assert len(winners) == 1, "两个 worker 同时抢同一任务，只能有一个成功"


@requires_db
async def test_reclaim_requeues_then_dead_letters(sm, db) -> None:  # noqa: ANN001
    cfg = TaskSettings()
    task = await service.create_task(db, await _create())

    for attempt in range(1, cfg.reclaim_max + 1):
        await service.lease(db, task.id, LeaseIn(device_id=f"mac-{attempt}"))
        await db.execute(
            sa.text("UPDATE tasks SET leased_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task.id},
        )
        await db.commit()
        result = await service.reclaim_expired(db, settings=cfg)
        assert result["reclaimed"] == 1

        await db.refresh(task)
        assert task.reclaim_count == attempt

    await db.refresh(task)
    assert task.state == TaskState.DEAD.value

    letters, count = await service.list_dead_letters(db)
    assert count == 1
    assert letters[0].reason == "lease_reclaimed"
    assert letters[0].task_id == task.id


@requires_db
async def test_reclaim_dry_run_does_not_write(sm, db) -> None:  # noqa: ANN001
    task = await service.create_task(db, await _create())
    await service.lease(db, task.id, LeaseIn(device_id="mac-1"))
    await db.execute(
        sa.text("UPDATE tasks SET leased_until = now() - interval '1 second' WHERE id = :id"),
        {"id": task.id},
    )
    await db.commit()

    result = await service.reclaim_expired(db, dry_run=True)
    assert result["reclaimed"] == 1
    assert result["ids"] == [str(task.id)]

    await db.refresh(task)
    assert task.state == TaskState.LEASED.value
    assert task.reclaim_count == 0


@requires_db
async def test_fail_retries_with_backoff_then_dead(api, db) -> None:  # noqa: ANN001
    created = (
        await api.post("/api/v1/tasks", json={"title": "会失败", "max_retries": 2})
    ).json()

    for attempt in range(1, 4):
        leased = await api.post(
            f"/api/v1/tasks/{created['id']}/lease",
            json={"device_id": "mac-1", "caps": {}},
        )
        assert leased.status_code == 200
        token = leased.json()["lease_token"]
        failed = await api.post(
            f"/api/v1/tasks/{created['id']}/fail",
            json={"lease_token": token, "error": f"boom-{attempt}"},
        )
        assert failed.status_code == 200

        if attempt <= 2:
            assert failed.json()["state"] == "failed"
            assert failed.json()["next_retry_at"] is not None
            await db.execute(
                sa.text(
                    "UPDATE tasks SET next_retry_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": uuid.UUID(created["id"])},
            )
            await db.commit()
            promoted = await service.promote_due_retries(db)
            assert promoted == [uuid.UUID(created["id"])]
        else:
            # 次数耗尽 → 死信，且不再自动重投
            assert failed.json()["state"] == "dead"
            assert failed.json()["next_retry_at"] is None

    letters = (await api.get("/api/v1/tasks/dead")).json()
    assert letters["count"] == 1
    assert letters["items"][0]["reason"] == "max_retries_exhausted"


@requires_db
async def test_non_retryable_failure_goes_dead_immediately(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "确定性失败"})).json()
    leased = await api.post(
        f"/api/v1/tasks/{created['id']}/lease", json={"device_id": "mac-1", "caps": {}}
    )
    token = leased.json()["lease_token"]
    failed = await api.post(
        f"/api/v1/tasks/{created['id']}/fail",
        json={"lease_token": token, "error": "编译错误", "retryable": False},
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "dead"

    letters = (await api.get("/api/v1/tasks/dead")).json()
    assert letters["items"][0]["reason"] == "non_retryable"


@requires_db
async def test_requeue_revives_dead_and_resolves_letter(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "复活我"})).json()
    leased = await api.post(
        f"/api/v1/tasks/{created['id']}/lease", json={"device_id": "mac-1", "caps": {}}
    )
    token = leased.json()["lease_token"]
    await api.post(
        f"/api/v1/tasks/{created['id']}/fail",
        json={"lease_token": token, "error": "boom", "retryable": False},
    )

    revived = await api.post(
        f"/api/v1/tasks/{created['id']}/requeue", json={"reason": "误报，人工重投"}
    )
    assert revived.status_code == 200
    body = revived.json()
    assert body["state"] == "queued"
    assert body["retry_count"] == 0
    assert body["reclaim_count"] == 0

    assert (await api.get("/api/v1/tasks/dead")).json()["count"] == 0
    resolved = (
        await api.get("/api/v1/tasks/dead", params={"include_resolved": True})
    ).json()
    assert resolved["items"][0]["resolution"] == "requeue"

    again = await api.post(
        f"/api/v1/tasks/{created['id']}/requeue", json={"reason": "再复活一次"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "illegal_transition"


@requires_db
async def test_cancel_stops_queued_task(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "取消我"})).json()
    cancelled = await api.post(
        f"/api/v1/tasks/{created['id']}/cancel", json={"reason": "不做了"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    claim = await api.post("/api/v1/tasks/claim", json={"device_id": "mac-1", "caps": {}})
    assert claim.status_code == 204


@requires_db
async def test_candidates_without_devices_table(api) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "找设备"})).json()
    response = await api.get(f"/api/v1/tasks/{created['id']}/candidates")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == created["id"]
    assert body["candidates"] == []
    assert body["note"]


@requires_db
async def test_queued_overdue_flag(api, db) -> None:  # noqa: ANN001
    created = (await api.post("/api/v1/tasks", json={"title": "排太久了"})).json()
    await db.execute(
        sa.text("UPDATE tasks SET queued_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": uuid.UUID(created["id"])},
    )
    await db.commit()

    fetched = await api.get(f"/api/v1/tasks/{created['id']}")
    assert fetched.json()["queued_overdue"] is True
    assert fetched.json()["queued_seconds"] >= 3600

    overdue = await api.get("/api/v1/tasks", params={"overdue_only": True})
    assert overdue.json()["count"] == 1


@requires_db
async def test_budget_status_reads_preaggregates(api, db) -> None:  # noqa: ANN001
    first = (
        await api.post("/api/v1/tasks", json={"title": "有预算", "budget_cap": "100"})
    ).json()
    await api.post("/api/v1/tasks", json={"title": "止血", "budget_cap": "0"})
    await db.execute(
        sa.text("UPDATE tasks SET cost_spent = 40 WHERE id = :id"),
        {"id": uuid.UUID(first["id"])},
    )
    await db.commit()

    status = await api.get("/api/v1/tasks/budget-status")
    assert status.status_code == 200
    body = status.json()
    assert body["tasks_with_cap"] == 2
    assert body["tasks_held"] == 1
    assert Decimal(body["cap_total"]) == Decimal("100.0000")
    assert Decimal(body["spent_total"]) == Decimal("40.0000")
    assert Decimal(body["available_total"]) == Decimal("50.0000")


@requires_db
async def test_service_rejects_requeue_of_running_task(sm, db) -> None:  # noqa: ANN001
    task = await service.create_task(db, await _create())
    await service.lease(db, task.id, LeaseIn(device_id="mac-1"))
    with pytest.raises(ConflictError) as exc:
        await service.requeue(db, task.id, RequeueIn(reason="提前复活"))
    assert exc.value.code == "illegal_transition"


@requires_db
async def test_claim_fairness_inserts_low_priority(sm, db) -> None:  # noqa: ANN001
    for index in range(3):
        await service.create_task(db, TaskCreate(title=f"高优-{index}", priority=0))
    await service.create_task(db, TaskCreate(title="低优", priority=8))

    config = TaskSettings(fairness_burst=2)
    claimed: list[str] = []
    for _ in range(3):
        result = await service.claim(
            db, ClaimIn(device_id="mac-1", caps={}), settings=config
        )
        assert result is not None
        claimed.append(result[0].title)

    assert claimed[:2] == ["高优-0", "高优-1"]
    assert claimed[2] == "低优", "连续两个高优后必须插队一个低优任务（防饿死）"
