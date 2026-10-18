"""W04 设备接入模块测试。

测试策略（与 CI 一致：backend job 没有 Postgres/Redis）：
- 用 FakeRepo（内存实现，与生产 DeviceRepository 同方法签名）+ FakeClock（可控时钟，不 sleep）
  覆盖 service 层全部业务规则；
- 用 FastAPI dependency_overrides 注入，走真实 HTTP 栈（TestClient）验证路由、
  认证（Owner Bearer / 设备 HMAC）、错误码与响应结构；
- 数据库层约束（UNIQUE(node_id,seq)、UPSERT、保留 30 份的单条 SQL）由生产 repository +
  alembic 迁移保证，CI 的 migrations job 做迁移 roundtrip。

对应窗口契约：plan/W04.md（W04-I01…W04-I06）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_token, hash_device_token, sign_hmac
from app.main import create_app
from app.modules.devices.deps import get_device_service
from app.modules.devices.repository import DeviceRecord
from app.modules.devices.service import DeviceService

API = "/api/v1/devices"
T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
TS0 = int(T0.timestamp())


# =========================================================================
# 测试基础设施
# =========================================================================


class FakeClock:
    """可控时钟：service 的 now() 全部走它，测试里显式推进，绝不 sleep。"""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0, minutes: int = 0) -> None:
        self.now += timedelta(seconds=seconds, minutes=minutes)


class FakeRepo:
    """内存版 DeviceRepository：与生产实现保持同一方法签名（随 issue 逐步扩展）。"""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceRecord] = {}
        self.codes: dict[str, dict] = {}

    async def commit(self) -> None:
        return None

    # ------------------------------------------------------------- 设备（I01）

    async def get_device(self, node_id: str) -> DeviceRecord | None:
        return self.devices.get(node_id)

    async def get_device_by_fingerprint(self, fingerprint: str) -> DeviceRecord | None:
        for record in self.devices.values():
            if record.fingerprint == fingerprint:
                return record
        return None

    async def create_device(self, record: DeviceRecord) -> DeviceRecord:
        self.devices[record.node_id] = record
        return record

    async def update_device(self, node_id: str, **fields: object) -> DeviceRecord | None:
        record = self.devices.get(node_id)
        if record is None:
            return None
        updated = record.with_updates(**fields)
        self.devices[node_id] = updated
        return updated

    async def list_devices(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[DeviceRecord]:
        rows = [r for r in self.devices.values() if status is None or r.status == status]
        rows.sort(key=lambda r: r.node_id)
        return rows[offset : offset + limit]

    # ------------------------------------------------------------- 引导码（I01）

    async def create_bootstrap_code(self, code_hash: str, expires_at: datetime | None) -> None:
        self.codes[code_hash] = {"expires_at": expires_at, "used_at": None, "used_by_node": None}

    async def bootstrap_code_valid(self, code_hash: str, *, now: datetime) -> bool:
        row = self.codes.get(code_hash)
        if row is None or row["used_at"] is not None:
            return False
        return row["expires_at"] is None or row["expires_at"] > now

    async def mark_bootstrap_used(self, code_hash: str, node_id: str, *, now: datetime) -> bool:
        row = self.codes.get(code_hash)
        if row is None or row["used_at"] is not None:
            return False
        row["used_at"] = now
        row["used_by_node"] = node_id
        return True


class FakeState:
    """一次测试的全部可注入状态：clock + repo + service。"""

    def __init__(self, clock: FakeClock, repo: FakeRepo) -> None:
        self.clock = clock
        self.repo = repo
        self.service = DeviceService(repo, clock=clock)


def make_fake() -> FakeState:
    return FakeState(FakeClock(), FakeRepo())


@pytest.fixture()
def fake() -> FakeState:
    return make_fake()


@pytest.fixture()
def client(fake: FakeState) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_device_service] = lambda: fake.service
    return TestClient(app)


@pytest.fixture()
def owner_headers() -> dict[str, str]:
    token = create_token("owner-1", "access", extra={"role": "owner"})
    return {"Authorization": f"Bearer {token}"}


def issue_code(client: TestClient, owner_headers: dict[str, str]) -> str:
    r = client.post(f"{API}/bootstrap-codes", headers=owner_headers)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def register_device(
    client: TestClient,
    fake: FakeState,
    owner_headers: dict[str, str],
    node_id: str = "mac-1",
    *,
    fingerprint: str | None = None,
    trust: str = "managed",
    caps: list[str] | None = None,
    code: str | None = None,
) -> dict:
    payload = {
        "bootstrap_code": code or issue_code(client, owner_headers),
        "node_id": node_id,
        "fingerprint": fingerprint or f"fp-{node_id}",
        "caps": caps if caps is not None else ["gui", "orca"],
        "tolerance": 300,
        "squad": "alpha",
        "purpose": "dev",
        "trust": trust,
    }
    r = client.post(f"{API}/register", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def device_post(
    client: TestClient,
    path: str,
    token: str,
    payload: dict,
    *,
    node: str | None = None,
    ts: int | None = None,
    key: str | None = None,
):
    """以设备身份发 HMAC 签名请求（签名密钥 = sha256(明文 token)，与生产派生规则一致）。"""
    body = json.dumps(payload).encode()
    ts = TS0 if ts is None else ts
    sig = sign_hmac(key or hash_device_token(token), body, ts)
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Afc-Node": node or payload["node"],
            "X-Afc-Timestamp": str(ts),
            "X-Afc-Signature": sig,
        },
    )


# =========================================================================
# W04-I01 · 设备注册与 token
# =========================================================================


class TestRegister:
    def test_register_requires_bootstrap_code(self, client: TestClient, fake: FakeState) -> None:
        # 无凭据自助注册被拒绝：缺引导码 422，坏引导码 401
        r = client.post(f"{API}/register", json={"node_id": "mac-1", "fingerprint": "fp-mac-1"})
        assert r.status_code == 422
        r = client.post(
            f"{API}/register",
            json={"bootstrap_code": "no-such-code-000", "node_id": "mac-1", "fingerprint": "fp-mac-1"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

    def test_register_returns_token_once(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        body = register_device(client, fake, owner_headers, "mac-1")
        assert body["created"] is True
        token = body["token"]
        assert token and len(token) >= 32

        # 明文只出现一次：设备记录里只有 sha256
        record = fake.repo.devices["mac-1"]
        assert record.token_hash == hash_device_token(token)
        assert record.token_hash != token

        listed = client.get(f"{API}/", headers=owner_headers).json()
        assert len(listed) == 1
        assert "token" not in listed[0]
        assert "fingerprint" not in listed[0]  # 指纹不暴露给前端用于撞注册

    def test_register_idempotent_same_fingerprint(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        first = register_device(client, fake, owner_headers, "mac-1")
        second = register_device(client, fake, owner_headers, "mac-1")  # 同机同 node 重注册
        assert second["created"] is False
        assert second["token"] is None  # 不换发 token
        assert fake.repo.devices["mac-1"].token_hash == hash_device_token(first["token"])

    def test_register_fingerprint_conflict(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        register_device(client, fake, owner_headers, "mac-1", fingerprint="fp-same-machine")
        r = client.post(
            f"{API}/register",
            json={
                "bootstrap_code": issue_code(client, owner_headers),
                "node_id": "mac-2",
                "fingerprint": "fp-same-machine",  # 同一台机器伪装成第二个节点
            },
        )
        assert r.status_code == 409

    def test_register_node_id_taken(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        register_device(client, fake, owner_headers, "mac-1", fingerprint="fp-machine-a")
        r = client.post(
            f"{API}/register",
            json={
                "bootstrap_code": issue_code(client, owner_headers),
                "node_id": "mac-1",
                "fingerprint": "fp-machine-b",
            },
        )
        assert r.status_code == 409

    def test_bootstrap_code_is_single_use(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        code = issue_code(client, owner_headers)
        register_device(client, fake, owner_headers, "mac-1", code=code)
        r = client.post(
            f"{API}/register",
            json={"bootstrap_code": code, "node_id": "mac-2", "fingerprint": "fp-mac-2"},
        )
        assert r.status_code == 401  # 已消耗的引导码不能再用

    def test_register_third_party_trust(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        body = register_device(client, fake, owner_headers, "api-ext", trust="third_party", caps=["platform"])
        assert body["created"] is True
        assert fake.repo.devices["api-ext"].trust == "third_party"

    def test_patch_device_fields(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        register_device(client, fake, owner_headers, "mac-1")
        r = client.patch(
            f"{API}/mac-1",
            headers=owner_headers,
            json={"caps": ["gui"], "tolerance": 3600, "squad": "night", "purpose": "batch"},
        )
        assert r.status_code == 200
        record = fake.repo.devices["mac-1"]
        assert record.caps == ["gui"]
        assert record.tolerance == 3600
        assert record.squad == "night"

        r = client.patch(f"{API}/mac-1", headers=owner_headers, json={"trust": "bogus"})
        assert r.status_code == 422

    def test_revoke_device(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        register_device(client, fake, owner_headers, "mac-1")
        r = client.post(f"{API}/mac-1/revoke", headers=owner_headers)
        assert r.status_code == 200
        record = fake.repo.devices["mac-1"]
        assert record.status == "revoked"
        assert record.token_hash is None  # token 即刻失效
        assert record.revoked_at is not None

        r = client.post(f"{API}/mac-2/revoke", headers=owner_headers)
        assert r.status_code == 404

    def test_revoke_then_re_register_reactivates(self, client: TestClient, fake: FakeState, owner_headers) -> None:
        first = register_device(client, fake, owner_headers, "mac-1")
        client.post(f"{API}/mac-1/revoke", headers=owner_headers)
        body = register_device(client, fake, owner_headers, "mac-1")
        assert body["reactivated"] is True
        assert body["token"] and body["token"] != first["token"]
        record = fake.repo.devices["mac-1"]
        assert record.status == "offline"
        assert record.token_hash == hash_device_token(body["token"])
        assert record.revoked_at is None
