"""多维度限流（`/auth/*` 专用桶）。

与 `modules/identity/throttle.py` 的分工：
- `throttle.py` 只数「登录**失败**次数」（业务语义，5 次/15 分钟锁账号锁定）；
- 本文件数「请求**次数**」（传输层语义，防爆破/防刷），两者叠加生效。

两个维度必须同时存在，缺一个就有绕过路径：
- **IP 维度**：同一台机器轮换大量账号猜密码；
- **主体维度**：同一账号被僵尸网络从大量出口 IP 打。

计数落 Redis（多 worker 才有意义）；`dev`/`test` 退化为进程内计数，
理由是本机开发/CI 不该为跑一条单测起一个 Redis——**不是**让生产也走内存。
生产/预发必须 Redis：进程内计数在 `uvicorn --workers 4` 下等于把限额放大 4 倍。

键里不放邮箱明文（Redis 不该成为 PII 副本），只放 sha256 前 16 位指纹；
IP 直接放（它本来就在访问日志里）。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from .config import settings
from .errors import RateLimitedError
from .redis import get_redis

# 维度常量：写死成字面量，避免调用方把 "IP"/"ip" 拼错后静默少一个维度。
DIM_IP = "ip"
DIM_SUBJECT = "subject"


@dataclass(frozen=True, slots=True)
class Rule:
    """一个桶：`bucket` 进 Redis 键，`limit` 次 / `window_seconds` 秒。"""

    bucket: str
    limit: int
    window_seconds: int
    dimension: str = DIM_IP

    def __post_init__(self) -> None:
        if self.dimension not in (DIM_IP, DIM_SUBJECT):
            raise ValueError(f"未知限流维度: {self.dimension}")
        if self.limit <= 0 or self.window_seconds <= 0:
            raise ValueError("限流上限与窗口必须为正数")


@dataclass(frozen=True, slots=True)
class Decision:
    """一次计数的结果。`retry_after` 只在被拒时有意义，但始终 >= 1。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    key: str


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> Decision: ...


class RedisRateLimiter:
    """INCR + 首次设 TTL + TTL 一次 pipeline 拿全，避免三次往返。"""

    async def hit(self, key: str, limit: int, window_seconds: int) -> Decision:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
        count = int(count)
        # ttl 为 -1/-2（无过期/键不存在）时退回窗口长度，保证 Retry-After 不为 0。
        retry_after = int(ttl) if int(ttl) > 0 else window_seconds
        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=max(1, retry_after),
            key=key,
        )


class MemoryRateLimiter:
    """进程内固定窗口计数（dev/test）。语义与 Redis 版一致，只是不共享。"""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> Decision:
        now = time.monotonic()
        count, expires_at = self._buckets.get(key, (0, 0.0))
        if expires_at <= now:  # 窗口已过：从 1 重新计数
            count, expires_at = 0, now + window_seconds
        count += 1
        self._buckets[key] = (count, expires_at)
        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=max(1, math.ceil(expires_at - now)),
            key=key,
        )


def build_rate_limiter() -> RateLimiter:
    """生产/预发用 Redis；dev/test 用内存。选择依据只有 `app_env`，不新增配置项。"""
    if settings.app_env in ("staging", "prod"):
        return RedisRateLimiter()
    return MemoryRateLimiter()


# ---------------------------------------------------------------- 规则表
# 数字的来由：正常用户登录一次、刷新一次；异常流量是几十倍起步。
# 故意留出足够余量，让「业务限流」（失败 5 次/15 分钟）先触发，避免正常换设备被 429。
LOGIN_RULES: tuple[Rule, ...] = (
    Rule("auth_login_ip", 20, 60, DIM_IP),
    Rule("auth_login_subject", 10, 60, DIM_SUBJECT),
)
REFRESH_RULES: tuple[Rule, ...] = (
    Rule("auth_refresh_ip", 60, 60, DIM_IP),
    Rule("auth_refresh_subject", 30, 60, DIM_SUBJECT),
)
SESSION_RULES: tuple[Rule, ...] = (
    Rule("auth_session_ip", 120, 60, DIM_IP),
    Rule("auth_session_subject", 60, 60, DIM_SUBJECT),
)

# 键是 `/auth/` 之后的尾巴。新增端点忘了登记也不会裸奔：落到 DEFAULT。
_AUTH_RULES: dict[str, tuple[Rule, ...]] = {
    "login": LOGIN_RULES,
    "refresh": REFRESH_RULES,
    "logout": SESSION_RULES,
    "sessions": SESSION_RULES,
    "sessions/revoke-all": SESSION_RULES,
    "me": SESSION_RULES,
    "scopes": SESSION_RULES,
}
DEFAULT_AUTH_RULES: tuple[Rule, ...] = SESSION_RULES


def rules_for_path(path: str) -> tuple[Rule, ...]:
    """只对 `/auth/` 下的路径生效；其它模块不受本文件影响。"""
    marker = "/auth/"
    idx = path.find(marker)
    if idx == -1:
        return ()
    tail = path[idx + len(marker) :].strip("/")
    return _AUTH_RULES.get(tail, DEFAULT_AUTH_RULES)


# ---------------------------------------------------------------- 维度取值
def client_ip(request: Request) -> str:
    """最左侧 X-Forwarded-For（Caddy 反代会覆盖客户端伪造的值），否则用连接地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def fingerprint(value: str) -> str:
    """邮箱/令牌不进 Redis 键；16 位十六进制足够区分，且不可逆。"""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:16]


async def subject_of(request: Request) -> str | None:
    """本请求的「主体」指纹：优先取请求体里的邮箱/刷新令牌，其次取访问令牌的 sub。

    - `/auth/login`：入参邮箱（还没登录，只能看它）；
    - `/auth/refresh`：刷新令牌指纹（旧令牌泄露时同样该被限流）；
    - 已认证端点：访问令牌的 `sub`。

    这里只解码不判权（判权在 `deps`/`rbac`）：解不开就当没有主体维度，
    请求本身随后会在认证依赖处被 401，限流不该先于认证给出「令牌真假」信号。
    """
    body_subject = await _subject_from_body(request)
    if body_subject:
        return body_subject
    return _subject_from_token(request)


async def _subject_from_body(request: Request) -> str | None:
    if request.method not in ("POST", "PUT", "PATCH"):
        return None
    try:
        raw = await request.body()  # Starlette 会缓存，端点稍后仍能正常解析
    except Exception:  # noqa: BLE001 - 读不到就当没有主体
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for field in ("email", "refresh_token"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return fingerprint(value)
    return None


def _subject_from_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    from .security import TokenError, decode_token  # 局部导入：避免 core 内循环依赖

    try:
        claims = decode_token(token, expect="access")
    except TokenError:
        return None
    subject = str(claims.get("sub") or "").strip()
    return fingerprint(subject) if subject else None


# ---------------------------------------------------------------- 依赖
def limiter_of(request: Request) -> RateLimiter:
    """每个 app 实例一个 limiter（测试里各 app 互不串计数）。"""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = build_rate_limiter()
        request.app.state.rate_limiter = limiter
    return limiter


def rules_of(request: Request) -> tuple[Rule, ...]:
    overrides = getattr(request.app.state, "rate_limit_rules", None) or {}
    path = request.url.path
    if path in overrides:
        return tuple(overrides[path])
    return rules_for_path(path)


async def auth_rate_limit(request: Request) -> None:
    """`/auth/*` 的入口守卫：任一维度超限即 429，并带 `Retry-After`。

    两个桶都计数、但只有一个能触发拒绝——不能因为 IP 桶还宽松就跳过主体桶计数，
    否则「先刷满 IP 桶再看主体桶」就成了绕过手段。
    """
    rules = rules_of(request)
    if not rules:
        return

    limiter = limiter_of(request)
    ip = client_ip(request)
    needs_subject = any(rule.dimension == DIM_SUBJECT for rule in rules)
    subject = await subject_of(request) if needs_subject else None

    for rule in rules:
        value = ip if rule.dimension == DIM_IP else subject
        if not value:  # 主体未知（例如伪造令牌）时不计数，交给认证依赖拒绝
            continue
        decision = await limiter.hit(
            f"rl:{rule.bucket}:{value}", rule.limit, rule.window_seconds
        )
        if not decision.allowed:
            raise RateLimitedError(
                f"请求过于频繁（{rule.bucket}，{rule.window_seconds}s 内上限 {rule.limit} 次）",
                details={
                    "bucket": rule.bucket,
                    "dimension": rule.dimension,
                    "limit": rule.limit,
                    "window_seconds": rule.window_seconds,
                    "retry_after": decision.retry_after,
                },
                headers={
                    "Retry-After": str(decision.retry_after),
                    "X-RateLimit-Limit": str(rule.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
