"""W01-I04 单测：`/auth/*` 双维度限流 + 生产暴露面收口。

覆盖的失败路径（验收 ①②）：
- IP 维度：同一出口连续打 → 第 N+1 次 429，且带 `Retry-After`；
- 主体维度：**换 IP 也拦得住**（这是「只按 IP 限流」的绕过路径）；
- 桶隔离：登录桶打爆不影响 `/auth/me` 的桶（否则一次爆破能把正常会话一起按死）；
- 窗口滚动：窗口过去后重新放行（不能永久锁死）；
- 生产禁用 `/docs`、`/openapi.json`，开发环境仍可用。

不依赖 PG/Redis：内存仓储 + 内存限流器，CI 的 smoke job 可直接跑。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.ratelimit import (
    DIM_IP,
    DIM_SUBJECT,
    MemoryRateLimiter,
    RedisRateLimiter,
    Rule,
    build_rate_limiter,
)
from app.main import create_app
from app.modules.identity.deps import get_identity_service
from app.modules.identity.repository import MemoryIdentityRepository
from app.modules.identity.service import IdentityService
from app.modules.identity.throttle import MemoryThrottle

EMAIL = "owner@example.com"
LOGIN_PATH = "/api/v1/auth/login"


def _app(rules: dict[str, tuple[Rule, ...]] | None = None) -> TestClient:
    """每个用例一个新 app：限流计数挂在 app.state 上，用例之间互不串味。"""
    app = create_app()
    service = IdentityService(MemoryIdentityRepository(), MemoryThrottle())
    app.dependency_overrides[get_identity_service] = lambda: service
    if rules:
        app.state.rate_limit_rules = rules
    return TestClient(app)


def _login(client: TestClient, email: str = EMAIL, ip: str = "203.0.113.7"):
    return client.post(
        LOGIN_PATH,
        json={"email": email, "password": "wrong-password"},
        headers={"x-forwarded-for": ip},
    )


@pytest.fixture()
def client() -> Iterator[TestClient]:
    yield _app()


# ------------------------------------------------------------------ IP 维度
def test_login_ip_bucket_returns_429_with_retry_after() -> None:
    client = _app({LOGIN_PATH: (Rule("t_login_ip", 2, 60, DIM_IP),)})

    assert _login(client).status_code == 401  # 第 1 次：业务上密码错
    assert _login(client).status_code == 401  # 第 2 次：仍在额度内
    blocked = _login(client)  # 第 3 次：超限

    assert blocked.status_code == 429
    body = blocked.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["details"]["bucket"] == "t_login_ip"
    assert body["details"]["dimension"] == DIM_IP
    assert body["request_id"]
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.headers["x-ratelimit-limit"] == "2"
    assert blocked.headers["x-ratelimit-remaining"] == "0"


def test_ip_bucket_is_per_ip_not_global() -> None:
    client = _app({LOGIN_PATH: (Rule("t_login_ip", 1, 60, DIM_IP),)})

    assert _login(client, ip="198.51.100.1").status_code == 401
    assert _login(client, ip="198.51.100.1").status_code == 429
    # 换一个 IP：同一账号不该被别人打爆的桶连坐
    assert _login(client, ip="198.51.100.2").status_code == 401


# ------------------------------------------------------------------ 主体维度
def test_subject_bucket_blocks_across_rotating_ips() -> None:
    """只有 IP 限流时的绕过路径：换 IP 继续猜同一账号。主体维度必须独立拦住。"""
    client = _app({LOGIN_PATH: (Rule("t_login_subject", 1, 60, DIM_SUBJECT),)})

    assert _login(client, ip="198.51.100.10").status_code == 401
    blocked = _login(client, ip="198.51.100.11")

    assert blocked.status_code == 429
    assert blocked.json()["error"]["details"]["dimension"] == DIM_SUBJECT


def test_subject_bucket_does_not_leak_between_accounts() -> None:
    client = _app({LOGIN_PATH: (Rule("t_login_subject", 1, 60, DIM_SUBJECT),)})

    assert _login(client, email="a@example.com", ip="198.51.100.20").status_code == 401
    assert _login(client, email="b@example.com", ip="198.51.100.21").status_code == 401
    blocked = _login(client, email="a@example.com", ip="198.51.100.22")
    assert blocked.status_code == 429


def test_subject_key_does_not_store_plain_email() -> None:
    """Redis 键里不能出现邮箱明文（PII 不该有第二份副本）。"""
    client = _app({LOGIN_PATH: (Rule("t_login_subject", 5, 60, DIM_SUBJECT),)})
    _login(client, email="Owner@Example.com")
    _login(client, email="owner@example.com")  # 大小写不同，仍是同一个桶

    limiter = client.app.state.rate_limiter
    keys = list(limiter._buckets)  # type: ignore[attr-defined]
    assert keys, "主体维度没有计数，测试本身失效"
    assert all("example.com" not in key for key in keys)
    assert len(keys) == 1  # 规范化后同桶


# ------------------------------------------------------------------ 桶隔离
def test_login_bucket_does_not_block_other_auth_endpoints() -> None:
    client = _app({LOGIN_PATH: (Rule("t_login_ip", 1, 60, DIM_IP),)})

    assert _login(client).status_code == 401
    assert _login(client).status_code == 429
    # /auth/me 走自己的桶：爆破登录不该把已登录用户的会话一起按死
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "unauthorized"


def test_non_auth_paths_are_not_limited() -> None:
    client = _app({LOGIN_PATH: (Rule("t_login_ip", 1, 60, DIM_IP),)})
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_refresh_and_session_rules_are_registered() -> None:
    """新增 `/auth/*` 端点忘登记也不会裸奔：落到 DEFAULT（有额度、有主体维度）。"""
    from app.core.ratelimit import DEFAULT_AUTH_RULES, rules_for_path

    assert rules_for_path("/api/v1/auth/sessions/revoke-all")
    assert rules_for_path("/api/v1/auth/unknown-endpoint") == DEFAULT_AUTH_RULES
    assert {rule.dimension for rule in rules_for_path(LOGIN_PATH)} == {DIM_IP, DIM_SUBJECT}
    assert rules_for_path("/api/v1/tasks") == ()


# ------------------------------------------------------------------ 窗口滚动
async def test_memory_limiter_window_rolls_over() -> None:
    limiter = MemoryRateLimiter()
    key = "rl:t:ip:1.2.3.4"

    assert (await limiter.hit(key, limit=1, window_seconds=60)).allowed
    denied = await limiter.hit(key, limit=1, window_seconds=60)
    assert not denied.allowed and denied.retry_after >= 1

    # 模拟窗口过去（不 sleep，也不改系统时钟）
    count, _ = limiter._buckets[key]
    limiter._buckets[key] = (count, 0.0)
    assert (await limiter.hit(key, limit=1, window_seconds=60)).allowed


def test_limiter_backend_follows_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env, expected in (("prod", RedisRateLimiter), ("staging", RedisRateLimiter), ("dev", MemoryRateLimiter)):
        monkeypatch.setattr(settings, "app_env", env)
        assert isinstance(build_rate_limiter(), expected), env


# ------------------------------------------------------------------ 暴露面收口
def test_prod_disables_docs_and_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "prod")
    client = TestClient(create_app())

    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 404, f"{path} 在生产环境仍然可达"
        assert r.json()["error"]["code"] == "not_found"


def test_dev_keeps_docs_for_local_work() -> None:
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_app_does_not_emit_csp_or_hsts() -> None:
    """边界：CSP/HSTS 只能由 Caddy 给（见 docs/adr/0002-auth-model.md §6）。

    应用层若也发一份，本地 HTTP 开发会被 HSTS 粘住、两处策略还会互相覆盖。
    """
    client = TestClient(create_app())
    headers = client.get("/health").headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "content-security-policy" not in headers
    assert "strict-transport-security" not in headers
