"""W07 闸门测试（test_approvals.py）—— 占位文件已在 W07-I03 落地时替换。

边界打在：四分级、L0/L1 日限额转明日、L2/L3 不进队列、重复决策 409、
拒绝即记录、夜间 L0 静默（待明日确认）、熔断四条触发条件、阈值可从环境变量改。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ConflictError, NotFoundError
from app.core.security import create_token
from app.main import create_app
from app.modules.approvals import breaker, service
from app.modules.approvals.models import Approval
from app.modules.approvals.router import get_approval_repo
from app.modules.ledger.models import AttentionEntry
from app.modules.ledger.router import get_ledger_repo
from tests.test_ledger import FakeLedgerRepo

AUTH = {"Authorization": f"Bearer {create_token('owner-1', 'access')}"}

DAY = datetime(2026, 10, 18, 10, 30, tzinfo=UTC)  # 本地 18:30，非静默窗口
NIGHT = datetime(2026, 10, 18, 16, 30, tzinfo=UTC)  # 本地 00:30，静默窗口


# ------------------------------------------------------------------ 假仓库


class FakeApprovalRepo:
    def __init__(self) -> None:
        self.approvals: list[Approval] = []
        self.now = DAY  # 伪时钟：add() 里补 created_at 用
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def add(self, approval: Approval) -> Approval:
        if approval.id is None:
            approval.id = uuid.uuid4()
        if approval.created_at is None:
            approval.created_at = self.now
        if approval.visible_at is None:
            approval.visible_at = self.now
        self.approvals.append(approval)
        return approval

    async def get(self, approval_id) -> Approval | None:
        return next((a for a in self.approvals if a.id == approval_id), None)

    async def count_created_between(self, level, start, end) -> int:
        return sum(
            1
            for a in self.approvals
            if a.level == level and start <= a.created_at < end
        )

    async def queue(self, *, levels, now, limit=50):
        return [
            a
            for a in self.approvals
            if a.status == "pending" and a.level in levels and a.visible_at <= now
        ][:limit]

    async def decision_stats(self, start, end) -> tuple[int, int]:
        approved = rejected = 0
        for a in self.approvals:
            if (
                a.status == "decided"
                and a.decided_at is not None
                and start <= a.decided_at < end
            ):
                if a.decision == "approved":
                    approved += 1
                elif a.decision == "rejected":
                    rejected += 1
        return approved, rejected

    async def decided_today(self, now) -> int:
        start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        return sum(
            1
            for a in self.approvals
            if a.status == "decided"
            and a.decided_at is not None
            and a.decided_at >= start
        )

    async def decisions_by_day(self, days, now) -> dict[date, int]:
        out: dict[date, int] = {}
        for a in self.approvals:
            if a.status == "decided" and a.decided_at is not None:
                d = a.decided_at.date()
                out[d] = out.get(d, 0) + 1
        return out


def make_client(fake: FakeApprovalRepo, ledger_fake: FakeLedgerRepo | None = None):
    app = create_app()
    ledger_fake = ledger_fake or FakeLedgerRepo()
    app.dependency_overrides[get_approval_repo] = lambda: fake
    app.dependency_overrides[get_ledger_repo] = lambda: ledger_fake
    return TestClient(app), ledger_fake


def approval_payload(**overrides) -> dict:
    payload = {"level": "L1", "title": "CI 连败 3 次，是否回滚？"}
    payload.update(overrides)
    return payload


def _decided(level: str, decided_at: datetime, decision: str) -> Approval:
    return Approval(
        id=uuid.uuid4(),
        level=level,
        title="t",
        payload=None,
        status="decided",
        visible_at=decided_at,
        created_at=decided_at,
        decided_at=decided_at,
        decision=decision,
        decider="owner-1",
    )


def _gate_minutes(day_offset: int, minutes: str) -> AttentionEntry:
    return AttentionEntry(
        id=uuid.uuid4(),
        occurred_at=DAY - timedelta(days=day_offset),
        minutes=Decimal(minutes),
        source="gate",
        source_id=f"seed-{day_offset}-{minutes}",
        created_at=DAY - timedelta(days=day_offset),
    )


# ----------------------------------------------------------- 纯函数：熔断


def test_breaker_daily_decisions_over_limit() -> None:
    cfg = breaker.BreakerConfig()
    verdict = breaker.evaluate(breaker.BreakerInput(decisions_today=21, now_hour=12), cfg)
    assert verdict.defer_non_critical is True
    assert "daily_decisions_21_gt_20" in verdict.reasons
    # 恰好 20 不触发（严格大于）
    assert (
        breaker.evaluate(
            breaker.BreakerInput(decisions_today=20, now_hour=12)
        ).defer_non_critical
        is False
    )


def test_breaker_duration_rising_cuts_dispatch() -> None:
    rising = (1.0, 1.4, 1.6)  # 连升且累计 +60% ≥ 50%
    verdict = breaker.evaluate(
        breaker.BreakerInput(decisions_today=0, daily_avg_minutes=rising, now_hour=12)
    )
    assert verdict.dispatch_factor == Decimal("0.7")
    assert verdict.defer_non_critical is False
    # 涨幅 40% < 50% → 不触发
    verdict = breaker.evaluate(
        breaker.BreakerInput(decisions_today=0, daily_avg_minutes=(1.0, 1.2, 1.4), now_hour=12)
    )
    assert verdict.dispatch_factor == Decimal("1.0")
    # 中途下降 → 不触发
    verdict = breaker.evaluate(
        breaker.BreakerInput(decisions_today=0, daily_avg_minutes=(1.0, 1.6, 1.4), now_hour=12)
    )
    assert verdict.dispatch_factor == Decimal("1.0")


def test_breaker_veto_spike_marks_fatigue() -> None:
    verdict = breaker.evaluate(
        breaker.BreakerInput(
            decisions_today=0,
            veto_rate_recent=Decimal("0.40"),
            veto_rate_baseline=Decimal("0.10"),
            now_hour=12,
        )
    )
    assert verdict.fatigue_suspected is True
    assert verdict.review_sample == 3
    # 基线本来就高（否决率 30%）不算突增
    verdict = breaker.evaluate(
        breaker.BreakerInput(
            decisions_today=0,
            veto_rate_recent=Decimal("0.45"),
            veto_rate_baseline=Decimal("0.30"),
            now_hour=12,
        )
    )
    assert verdict.fatigue_suspected is False


def test_breaker_night_window_crosses_midnight() -> None:
    cfg = breaker.BreakerConfig()
    for hour in (23, 0, 3, 6):
        assert breaker.is_night(hour, cfg) is True, hour
    for hour in (7, 12, 22):
        assert breaker.is_night(hour, cfg) is False, hour
    verdict = breaker.evaluate(breaker.BreakerInput(decisions_today=0, now_hour=23))
    assert verdict.night_hold_l0 is True


def test_breaker_thresholds_from_env(monkeypatch) -> None:
    # 阈值全部来自配置且可改：环境变量改掉后判定翻转
    monkeypatch.setenv("AFC_BREAKER_DAILY_DECISION_LIMIT", "5")
    monkeypatch.setenv("AFC_BREAKER_DURATION_RISE_PCT", "0.2")
    cfg = breaker.load_breaker_config()
    assert cfg.daily_decision_limit == 5
    assert cfg.duration_rise_pct == Decimal("0.2")
    verdict = breaker.evaluate(breaker.BreakerInput(decisions_today=6, now_hour=12), cfg)
    assert verdict.defer_non_critical is True
    # 涨幅 40%：默认阈值(50%)不触发，改成 20% 后触发 → 派活量降 30%
    verdict = breaker.evaluate(
        breaker.BreakerInput(decisions_today=0, daily_avg_minutes=(1.0, 1.2, 1.4), now_hour=12),
        cfg,
    )
    assert verdict.dispatch_factor == Decimal("0.7")


def test_local_hour_offset() -> None:
    assert breaker.local_hour(16, 8) == 0  # UTC 16:00 = 本地 00:00


# ------------------------------------------------------ 服务层：限额与队列


@pytest.mark.asyncio
async def test_l0_daily_cap_defers_third_to_tomorrow() -> None:
    repo = FakeApprovalRepo()
    outs = [
        await service.create_approval(
            repo, service.ApprovalCreate(level="L0", title=f"t{i}"), now=DAY
        )
        for i in range(3)
    ]
    deferred = [d for _, d in outs]
    assert deferred == [False, False, True]  # L0 ≤2/日，第 3 个转明日
    third = outs[2][0]
    assert third.visible_at > third.created_at
    # 转明日的今天不进队列
    visible = await service.queue(repo, now=DAY)
    assert all(a.id != third.id for a in visible)


@pytest.mark.asyncio
async def test_l1_daily_cap_and_machine_levels_uncapped() -> None:
    repo = FakeApprovalRepo()
    outs = [
        (
            await service.create_approval(
                repo, service.ApprovalCreate(level="L1", title=f"t{i}"), now=DAY
            )
        )[1]
        for i in range(9)
    ]
    assert outs == [False] * 8 + [True]  # L1 ≤8/日
    # L2/L3 是机器级，无日限额
    _, d2 = await service.create_approval(
        repo, service.ApprovalCreate(level="L2", title="m1"), now=DAY
    )
    _, d3 = await service.create_approval(
        repo, service.ApprovalCreate(level="L3", title="m2"), now=DAY
    )
    assert d2 is False and d3 is False


@pytest.mark.asyncio
async def test_queue_excludes_l2_l3_by_default() -> None:
    repo = FakeApprovalRepo()
    for level in ("L0", "L1", "L2", "L3"):
        await service.create_approval(
            repo, service.ApprovalCreate(level=level, title="t"), now=DAY
        )
    default_queue = await service.queue(repo, now=DAY)
    assert {a.level for a in default_queue} == {"L0", "L1"}  # L2/L3 不进队列
    sampled = await service.queue(repo, levels=("L2", "L3"), now=DAY)  # 抽检页
    assert {a.level for a in sampled} == {"L2", "L3"}


# ---------------------------------------------------------- 服务层：决策


@pytest.mark.asyncio
async def test_decide_records_decision_fields() -> None:
    repo = FakeApprovalRepo()
    approval, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L1", title="回滚?"), now=DAY
    )
    result = await service.decide(
        repo, approval.id, service.DecideIn(decision="approve"), decider="owner-1", now=DAY
    )
    assert result.held is False
    a = result.approval
    assert a.status == "decided"
    assert a.decision == "approved"
    assert a.decided_at == DAY
    assert a.decider == "owner-1"
    assert a.reason is None  # reason 可为空但字段保留


@pytest.mark.asyncio
async def test_decide_reject_is_recorded_not_dropped() -> None:
    # 拒绝即记录：rejected 是一等决策，不是删除
    repo = FakeApprovalRepo()
    approval, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L1", title="删库?"), now=DAY
    )
    result = await service.decide(
        repo,
        approval.id,
        service.DecideIn(decision="reject", reason="证据不足"),
        decider="owner-1",
        now=DAY,
    )
    assert result.approval.decision == "rejected"
    assert result.approval.reason == "证据不足"


@pytest.mark.asyncio
async def test_decide_twice_conflicts_and_missing_404s() -> None:
    repo = FakeApprovalRepo()
    approval, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L1", title="t"), now=DAY
    )
    payload = service.DecideIn(decision="approve")
    await service.decide(repo, approval.id, payload, decider="owner-1", now=DAY)
    with pytest.raises(ConflictError):
        await service.decide(repo, approval.id, payload, decider="owner-1", now=DAY)
    with pytest.raises(NotFoundError):
        await service.decide(repo, uuid.uuid4(), payload, decider="owner-1", now=DAY)


@pytest.mark.asyncio
async def test_decide_l0_at_night_holds_for_morning() -> None:
    repo = FakeApprovalRepo()
    approval, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L0", title="战略转向?"), now=DAY
    )
    result = await service.decide(
        repo,
        approval.id,
        service.DecideIn(decision="approve"),
        decider="owner-1",
        now=NIGHT,  # 本地 00:30，静默窗口
    )
    assert result.held is True
    a = result.approval
    # 只记录"待明日确认"，不给结论
    assert a.status == "pending"
    assert a.decision is None and a.decided_at is None
    assert a.held_reason == "night_window"
    assert a.held_at == NIGHT
    # L1 夜间照常可决策
    l1, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L1", title="夜间例检"), now=DAY
    )
    result = await service.decide(
        repo, l1.id, service.DecideIn(decision="approve"), decider="owner-1", now=NIGHT
    )
    assert result.held is False


@pytest.mark.asyncio
async def test_decide_minutes_recorded_to_attention_ledger() -> None:
    repo = FakeApprovalRepo()
    ledger = FakeLedgerRepo()
    approval, _ = await service.create_approval(
        repo, service.ApprovalCreate(level="L1", title="t"), now=DAY
    )
    await service.decide(
        repo,
        approval.id,
        service.DecideIn(decision="approve", minutes=5),
        decider="owner-1",
        ledger_repo=ledger,
        now=DAY,
    )
    assert len(ledger.attention) == 1
    entry = ledger.attention[0]
    assert entry.source == "gate"
    assert entry.source_id == f"approval:{approval.id}"
    assert entry.minutes == Decimal("5")


@pytest.mark.asyncio
async def test_bandwidth_status_assembles_verdict() -> None:
    """一天把四条规则全部踩满：21 决策、耗时连升、否决率 10%→84%、白天。"""
    repo = FakeApprovalRepo()
    ledger = FakeLedgerRepo()
    # 今天：21 条被拒（顺带撑起否决率）；闸门 50 分钟
    for _ in range(21):
        repo.approvals.append(_decided("L1", DAY, "rejected"))
    ledger.attention.append(_gate_minutes(0, "50"))
    # 昨天：7 条被拒，闸门 14 分钟
    for _ in range(7):
        repo.approvals.append(_decided("L1", DAY - timedelta(days=1), "rejected"))
    ledger.attention.append(_gate_minutes(1, "14"))
    # 前天：4 拒 6 过，闸门 10 分钟
    for i in range(10):
        repo.approvals.append(
            _decided("L1", DAY - timedelta(days=2), "rejected" if i < 4 else "approved")
        )
    ledger.attention.append(_gate_minutes(2, "10"))
    # 基线周（8 天前）：1 拒 9 过 → 否决率 10%
    for i in range(10):
        repo.approvals.append(
            _decided("L1", DAY - timedelta(days=8), "rejected" if i < 1 else "approved")
        )

    config, inp, verdict = await service.bandwidth_status(repo, ledger, now=DAY)
    assert inp.decisions_today == 21
    # 日均耗时：10/10=1.0 → 14/7=2.0 → 50/21≈2.38，连升且 +138%
    assert inp.daily_avg_minutes == (1.0, 2.0, 50 / 21)
    assert inp.veto_rate_recent >= Decimal("0.40")
    assert inp.veto_rate_baseline == Decimal("0.1000")
    assert verdict.defer_non_critical is True
    assert verdict.dispatch_factor == Decimal("0.7")
    assert verdict.fatigue_suspected is True
    assert verdict.review_sample == 3
    assert verdict.night_hold_l0 is False  # 本地 18:30 非静默


# ------------------------------------------------------------------ HTTP 层


def test_router_create_queue_decide() -> None:
    fake = FakeApprovalRepo()
    client, _ = make_client(fake)
    r = client.post("/api/v1/approvals", json=approval_payload(), headers=AUTH)
    assert r.status_code == 201, r.text
    approval_id = r.json()["approval"]["id"]
    assert r.json()["deferred"] is False

    r = client.get("/api/v1/approvals/queue", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["count"] == 1

    r = client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "reject", "reason": "不同意", "minutes": 3},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["held"] is False
    assert r.json()["approval"]["decision"] == "rejected"

    # 重复决策 409；不存在 404
    r = client.post(
        f"/api/v1/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=AUTH
    )
    assert r.status_code == 409
    r = client.post(
        f"/api/v1/approvals/{uuid.uuid4()}/decide", json={"decision": "approve"}, headers=AUTH
    )
    assert r.status_code == 404


def test_router_decide_records_attention_minutes() -> None:
    fake = FakeApprovalRepo()
    client, ledger_fake = make_client(fake)
    approval_id = client.post(
        "/api/v1/approvals", json=approval_payload(), headers=AUTH
    ).json()["approval"]["id"]
    r = client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "minutes": 3},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert len(ledger_fake.attention) == 1
    assert ledger_fake.attention[0].source == "gate"


def test_router_queue_levels_param_for_sampling_page() -> None:
    fake = FakeApprovalRepo()
    client, _ = make_client(fake)
    client.post("/api/v1/approvals", json=approval_payload(level="L0", title="战略"), headers=AUTH)
    client.post("/api/v1/approvals", json=approval_payload(level="L2", title="机器默认"), headers=AUTH)
    default = client.get("/api/v1/approvals/queue", headers=AUTH).json()
    assert {i["level"] for i in default["items"]} == {"L0"}
    sampled = client.get(
        "/api/v1/approvals/queue", params={"levels": "L2,L3"}, headers=AUTH
    ).json()
    assert {i["level"] for i in sampled["items"]} == {"L2"}
    bad = client.get("/api/v1/approvals/queue", params={"levels": "L9"}, headers=AUTH)
    assert bad.status_code == 422


def test_router_l0_night_decide_returns_202(monkeypatch) -> None:
    fake = FakeApprovalRepo()
    client, _ = make_client(fake)
    monkeypatch.setattr(service, "utcnow", lambda: NIGHT)
    r = client.post("/api/v1/approvals", json=approval_payload(level="L0", title="战略"), headers=AUTH)
    approval_id = r.json()["approval"]["id"]
    r = client.post(
        f"/api/v1/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=AUTH
    )
    assert r.status_code == 202
    assert r.json()["held"] is True
    assert r.json()["approval"]["status"] == "pending"
    assert r.json()["approval"]["held_reason"] == "night_window"


def test_router_bandwidth_endpoint() -> None:
    fake = FakeApprovalRepo()
    client, ledger_fake = make_client(fake)
    fake.approvals.append(_decided("L1", fake.now, "approved"))
    r = client.get("/api/v1/approvals/bandwidth", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["daily_decision_limit"] == 20
    assert body["verdict"]["defer_non_critical"] is False
    assert isinstance(body["input"]["daily_avg_minutes"], list)


def test_router_unauthorized() -> None:
    client, _ = make_client(FakeApprovalRepo())
    assert client.get("/api/v1/approvals/queue").status_code == 401
    assert client.post("/api/v1/approvals", json=approval_payload()).status_code == 401
