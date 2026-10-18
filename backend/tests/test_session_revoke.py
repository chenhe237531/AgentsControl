"""W01-I03 单测：会话与设备吊销联动。

验收对照：
- ① 登出即吊销当前会话的刷新令牌 → `test_logout_revokes_only_current_session`
- ② 支持「吊销全部会话」→ `test_revoke_all_*`（含 keep_current 两种取值、缺 sid 的失败路径）
- ③ 统一的 `revoked_at` 约定与 `auth_events` 事件表 → `test_device_revoked_*`、`test_auth_events_*`
- ④ 吊销后旧 access token 仍可用 ≤15 min → `test_access_token_survives_logout_until_expiry`

全部用例不依赖 PG/Redis：`MemoryIdentityRepository` + `MemoryThrottle` 替换依赖。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_token
from app.main import create_app
from app.modules.identity.deps import get_identity_service
from app.modules.identity.repository import Credential, MemoryIdentityRepository
from app.modules.identity.service import IdentityService, credential_from_plain
from app.modules.identity.throttle import MemoryThrottle

EMAIL = "owner@example.com"
OTHER_EMAIL = "auditor@example.com"
PASSWORD = "correct-horse-battery-staple"
SUBJECT = "owner-1"
OTHER_SUBJECT = "owner-2"


class Harness:
    def __init__(self) -> None:
        creds = [
            Credential(
                subject=SUBJECT,
                email=EMAIL,
                password_hash=credential_from_plain(EMAIL, PASSWORD, subject=SUBJECT).password_hash,
                role="owner",
                scopes=("tasks:write",),
            ),
            Credential(
                subject=OTHER_SUBJECT,
                email=OTHER_EMAIL,
                password_hash=credential_from_plain(
                    OTHER_EMAIL, PASSWORD, subject=OTHER_SUBJECT
                ).password_hash,
                role="owner",
                scopes=(),
            ),
        ]
        self.repo = MemoryIdentityRepository(creds)
        self.throttle = MemoryThrottle()
        self.service = IdentityService(self.repo, self.throttle)
        app = create_app()
        app.dependency_overrides[get_identity_service] = lambda: self.service
        self.client = TestClient(app)

    def login(self, email: str = EMAIL) -> dict:
        r = self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()

    def refresh(self, refresh_token: str):
        return self.client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    def revoke_all(self, access_token: str, *, keep_current: bool = True):
        return self.client.post(
            "/api/v1/auth/sessions/revoke-all",
            headers=_bearer(access_token),
            json={"keep_current": keep_current},
        )


@pytest.fixture()
def h() -> Iterator[Harness]:
    yield Harness()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _events(h: Harness) -> list[str]:
    return [e.event for e in h.repo.events]


# ------------------------------------------------------------------ ① 登出
def test_logout_revokes_only_current_session(h: Harness) -> None:
    a = h.login()
    b = h.login()
    assert a["session_id"] != b["session_id"]

    out = h.client.post("/api/v1/auth/logout", headers=_bearer(a["access_token"]))
    assert out.status_code == 204

    dead = h.refresh(a["refresh_token"])
    assert dead.status_code == 401
    assert dead.json()["error"]["code"] == "session_revoked"

    # 另一个会话不受影响——登出是「这个设备」的事，不是「这个账号」的事
    alive = h.refresh(b["refresh_token"])
    assert alive.status_code == 200, alive.text


def test_logout_without_session_identity_is_401(h: Harness) -> None:
    token = create_token(SUBJECT, "access", extra={"role": "owner"})  # 没有 sid
    r = h.client.post("/api/v1/auth/logout", headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


# ------------------------------------------------------------------ ④ 访问令牌残留
def test_access_token_survives_logout_until_expiry(h: Harness) -> None:
    """设计取舍（必须写进文档）：吊销只作用于刷新令牌，access token 最多再活 15 分钟。

    如果哪天有人「顺手」让吊销同时废掉 access token，这个用例会红——
    那意味着每个请求都要查库/查黑名单，是另一套架构决定，得先改 ADR。
    """
    body = h.login()
    assert body["expires_in"] == settings.access_token_ttl_seconds == 900

    assert h.client.post(
        "/api/v1/auth/logout", headers=_bearer(body["access_token"])
    ).status_code == 204

    # refresh 链路立即断掉
    assert h.refresh(body["refresh_token"]).json()["error"]["code"] == "session_revoked"

    # 但已签发的 access token 仍能用到自然过期（≤15 min）
    me = h.client.get("/api/v1/auth/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["session_id"] == body["session_id"]
    assert body["expires_in"] <= 15 * 60


# ------------------------------------------------------------------ ② 吊销全部
def test_revoke_all_keeps_current_by_default(h: Harness) -> None:
    a = h.login()
    b = h.login()

    r = h.revoke_all(b["access_token"])
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked_sessions": 1, "kept_session_id": b["session_id"]}

    assert h.refresh(a["refresh_token"]).json()["error"]["code"] == "session_revoked"
    assert h.refresh(b["refresh_token"]).status_code == 200


def test_revoke_all_can_revoke_self_too(h: Harness) -> None:
    a = h.login()
    b = h.login()

    r = h.revoke_all(b["access_token"], keep_current=False)
    assert r.json() == {"revoked_sessions": 2, "kept_session_id": None}

    for tok in (a["refresh_token"], b["refresh_token"]):
        assert h.refresh(tok).json()["error"]["code"] == "session_revoked"

    # 仍然遵守同一条取舍：access token 到期前还能用
    assert h.client.get("/api/v1/auth/me", headers=_bearer(b["access_token"])).status_code == 200


def test_revoke_all_is_idempotent(h: Harness) -> None:
    a = h.login()
    b = h.login()
    assert h.revoke_all(b["access_token"]).json()["revoked_sessions"] == 1
    # 第二次没有可吊销的会话 → 0，而不是报错（前端重试不该 500）
    again = h.revoke_all(b["access_token"])
    assert again.status_code == 200
    assert again.json()["revoked_sessions"] == 0
    assert h.refresh(a["refresh_token"]).status_code == 401


def test_revoke_all_without_sid_cannot_keep_current(h: Harness) -> None:
    token = create_token(SUBJECT, "access", extra={"role": "owner"})
    r = h.revoke_all(token)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


def test_revoke_all_requires_authentication(h: Harness) -> None:
    assert h.client.post("/api/v1/auth/sessions/revoke-all", json={}).status_code == 401


def test_revoke_all_cannot_touch_other_subjects(h: Harness) -> None:
    victim = h.login(OTHER_EMAIL)
    mine = h.login()

    assert h.revoke_all(mine["access_token"], keep_current=False).json()["revoked_sessions"] == 1

    # 另一个主体的会话毫发无损——主体取自令牌，不接受请求体传入
    assert h.refresh(victim["refresh_token"]).status_code == 200


# ------------------------------------------------------------------ 会话列表
def test_sessions_list_marks_current_and_revoked(h: Harness) -> None:
    a = h.login()
    b = h.login()
    h.client.post("/api/v1/auth/logout", headers=_bearer(a["access_token"]))

    r = h.client.get("/api/v1/auth/sessions", headers=_bearer(b["access_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["active"] == 1

    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id[b["session_id"]]["current"] is True
    assert by_id[a["session_id"]]["current"] is False
    assert by_id[a["session_id"]]["revoked_reason"] == "logout"
    assert by_id[a["session_id"]]["active"] is False
    assert by_id[a["session_id"]]["revoked_at"] is not None

    # 列表是给人看的：不许泄露任何令牌线索
    for item in body["sessions"]:
        assert not {"token_hash", "jti", "refresh_token", "access_token"} & set(item)


def test_sessions_list_is_subject_scoped(h: Harness) -> None:
    h.login(OTHER_EMAIL)
    mine = h.login()

    r = h.client.get("/api/v1/auth/sessions", headers=_bearer(mine["access_token"]))
    assert r.json()["total"] == 1
    assert r.json()["sessions"][0]["session_id"] == mine["session_id"]


def test_sessions_list_requires_auth(h: Harness) -> None:
    assert h.client.get("/api/v1/auth/sessions").status_code == 401


def test_viewer_can_manage_own_sessions(h: Harness) -> None:
    """自助操作不该要求管理员：viewer 也能看会话、踢掉其它设备。"""
    body = h.login()
    viewer = create_token(SUBJECT, "access", extra={"role": "viewer", "sid": "sess-viewer"})
    listed = h.client.get("/api/v1/auth/sessions", headers=_bearer(viewer))
    assert listed.status_code == 200
    revoked = h.revoke_all(viewer, keep_current=False)
    assert revoked.status_code == 200
    assert h.refresh(body["refresh_token"]).status_code == 401


# ------------------------------------------------------------------ ③ 设备吊销约定
def test_device_revoked_session_blocks_refresh_and_shows_reason(h: Harness) -> None:
    """W04 只读约定：设备侧把 `revoked_at` + `revoked_reason='device_revoked'` 写到会话上，
    身份侧无需改代码就能让该会话失去续期能力，并在会话列表里说明原因。
    """
    body = h.login()

    # 模拟 W04 吊销设备令牌：写 revoked_at，不碰别的列
    import asyncio

    asyncio.run(
        h.repo.revoke_session(body["session_id"], "device_revoked", datetime.now(UTC))
    )
    asyncio.run(h.repo.commit())

    r = h.refresh(body["refresh_token"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "session_revoked"

    listed = h.client.get("/api/v1/auth/sessions", headers=_bearer(body["access_token"]))
    session = listed.json()["sessions"][0]
    assert session["revoked_reason"] == "device_revoked"
    assert session["active"] is False


# ------------------------------------------------------------------ 事件表
def test_auth_events_record_login_refresh_logout(h: Harness) -> None:
    body = h.login()
    h.refresh(body["refresh_token"])
    h.client.post("/api/v1/auth/logout", headers=_bearer(body["access_token"]))

    assert _events(h) == ["login", "refresh", "logout"]
    assert all(e.subject == SUBJECT for e in h.repo.events)
    assert {e.session_id for e in h.repo.events} == {body["session_id"]}


def test_auth_events_record_replay_as_security_event(h: Harness) -> None:
    body = h.login()
    h.refresh(body["refresh_token"])
    h.refresh(body["refresh_token"])  # 重放

    assert "refresh_replayed" in _events(h)
    replay = [e for e in h.repo.events if e.event == "refresh_replayed"][0]
    assert replay.session_id == body["session_id"]


def test_auth_events_record_session_revoked(h: Harness) -> None:
    a = h.login()
    b = h.login()
    h.revoke_all(b["access_token"])

    revoked = [e for e in h.repo.events if e.event == "session_revoked"]
    assert [e.session_id for e in revoked] == [a["session_id"]]
    assert revoked[0].detail == "revoke_all"


def test_unknown_event_kind_is_rejected(h: Harness) -> None:
    """事件名漂移会让审计查询失效，所以在写入处直接失败，而不是静默落库。"""
    import asyncio

    with pytest.raises(ValueError, match="未知认证事件"):
        asyncio.run(h.service._record_event(SUBJECT, "some_typo_event"))
