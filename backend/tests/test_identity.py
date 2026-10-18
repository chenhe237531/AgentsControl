"""W01-I01 单测：登录与令牌轮换。

全部用例**不依赖 PG/Redis**：用 `MemoryIdentityRepository` + `MemoryThrottle`
通过 `dependency_overrides` 替换真实依赖，因此在 CI 的 smoke job 里也能跑。

覆盖的失败路径（验收 ⑤）：轮换、重放（含整链吊销）、过期、白名单外主体，
另加：错误密码、未知邮箱、限流、令牌类型混用、登出吊销。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_token
from app.main import create_app
from app.modules.identity.deps import get_identity_service
from app.modules.identity.repository import Credential, MemoryIdentityRepository
from app.modules.identity.service import IdentityService, credential_from_plain
from app.modules.identity.throttle import LOGIN_MAX_FAILURES, MemoryThrottle

EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery-staple"
SUBJECT = "owner-1"
SCOPES = ("tasks:write", "ledger:read")


class Harness:
    """一个「无 PG、无 Redis」的应用上下文：内存仓储 + 内存限流计数器。"""

    def __init__(self) -> None:
        self.repo = MemoryIdentityRepository(
            [
                Credential(
                    subject=SUBJECT,
                    email=EMAIL,
                    password_hash=credential_from_plain(
                        EMAIL, PASSWORD, subject=SUBJECT, scopes=SCOPES
                    ).password_hash,
                    role="owner",
                    scopes=SCOPES,
                )
            ]
        )
        self.throttle = MemoryThrottle()
        self.service = IdentityService(self.repo, self.throttle)
        app = create_app()
        app.dependency_overrides[get_identity_service] = lambda: self.service
        self.client = TestClient(app)

    def login(self, password: str = PASSWORD) -> dict:
        return self.client.post(
            "/api/v1/auth/login", json={"email": EMAIL, "password": password}
        ).json()


@pytest.fixture()
def h() -> Iterator[Harness]:
    yield Harness()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ 正常路径
def test_login_returns_token_pair(h: Harness) -> None:
    r = h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_ttl_seconds == 900
    assert body["refresh_expires_in"] == settings.refresh_token_ttl_seconds == 14 * 24 * 3600
    assert body["subject"] == SUBJECT
    assert body["scopes"] == list(SCOPES)

    me = h.client.get("/api/v1/auth/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json() == {
        "subject": SUBJECT,
        "role": "owner",
        "scopes": list(SCOPES),
        "email": EMAIL,
        "session_id": body["session_id"],
    }


def test_refresh_token_is_stored_hashed_only(h: Harness) -> None:
    body = h.login()
    assert len(h.repo.tokens) == 1
    stored = next(iter(h.repo.tokens.values()))
    assert stored.token_hash != body["refresh_token"]
    assert len(stored.token_hash) == 64
    assert not hasattr(stored, "token")  # 结构上就没有明文字段


# ------------------------------------------------------------------ 轮换
def test_refresh_rotates_and_new_token_works(h: Harness) -> None:
    first = h.login()
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["session_id"] == first["session_id"]  # 同一会话，不新开会话
    assert len(h.repo.tokens) == 2
    ok = h.client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert ok.status_code == 200


# ------------------------------------------------------------------ 重放
def test_replay_revokes_whole_session_chain(h: Harness) -> None:
    first = h.login()
    rotated = h.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    ).json()

    replay = h.client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_replayed"

    # 整链吊销：刚才那个「新的、本来有效」的令牌也不能用了
    after = h.client.post("/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "session_revoked"

    reasons = {r.revoked_reason for r in h.repo.tokens.values()}
    assert reasons == {"refresh_replayed"}
    assert all(r.revoked_at is not None for r in h.repo.tokens.values())


def test_revoked_session_cannot_refresh(h: Harness) -> None:
    first = h.login()
    out = h.client.post(
        "/api/v1/auth/logout",
        headers=_bearer(first["access_token"]),
        json={"refresh_token": first["refresh_token"]},
    )
    assert out.status_code == 204
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "session_revoked"


# ------------------------------------------------------------------ 过期 / 类型 / 伪造
def test_expired_refresh_token(h: Harness) -> None:
    expired = create_token(SUBJECT, "refresh", ttl_seconds=-1, extra={"sid": "s-1"})
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": expired})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "refresh_expired"


def test_access_token_cannot_be_used_as_refresh(h: Harness) -> None:
    access = h.login()["access_token"]
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


def test_unknown_or_forged_refresh_token(h: Harness) -> None:
    unknown = create_token(SUBJECT, "refresh", extra={"sid": "ghost"})
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": unknown})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"

    forged = h.client.post("/api/v1/auth/refresh", json={"refresh_token": "not.a.jwt.at-all"})
    assert forged.status_code == 401


# ------------------------------------------------------------------ 白名单
def test_subject_outside_whitelist_is_forbidden(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "owner_openids", "somebody-else")
    token = create_token("intruder", "access", extra={"role": "owner"})
    r = h.client.get("/api/v1/auth/me", headers=_bearer(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"

    # 白名单内则放行（证明 403 来自白名单，而不是别的分支）
    monkeypatch.setattr(settings, "owner_openids", SUBJECT)
    ok = h.client.get("/api/v1/auth/me", headers=_bearer(h.login()["access_token"]))
    assert ok.status_code == 200


# ------------------------------------------------------------------ 登录失败与限流
def test_wrong_password_and_unknown_email_are_indistinguishable(h: Harness) -> None:
    wrong = h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "nope"})
    ghost = h.client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json()["error"]["code"] == ghost.json()["error"]["code"] == "invalid_credentials"


def test_login_rate_limited_after_five_failures(h: Harness) -> None:
    for _ in range(LOGIN_MAX_FAILURES):
        r = h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "nope"})
        assert r.status_code == 401

    # 第 6 次即使密码正确也要拦（否则限流形同虚设）
    blocked = h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert blocked.json()["error"]["details"]["retry_after_seconds"] == 900
    assert h.repo.tokens == {}  # 被拦住时绝不签发令牌


def test_successful_login_resets_failure_counter(h: Harness) -> None:
    for _ in range(LOGIN_MAX_FAILURES - 1):
        h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "nope"})
    assert h.client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 200
    assert h.throttle.counters == {}

    for _ in range(LOGIN_MAX_FAILURES - 1):
        h.client.post("/api/v1/auth/login", json={"email": EMAIL, "password": "nope"})
    assert h.client.post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 200


# ------------------------------------------------------------------ 边界
def test_malformed_login_body_is_422(h: Harness) -> None:
    r = h.client.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_logout_requires_authentication(h: Harness) -> None:
    assert h.client.post("/api/v1/auth/logout").status_code == 401
