"""登录失败计数（限流）。

只做「计数 + 窗口」这一件事：真正的多维度限流（IP/主体/端点桶）在 W01-I04 的
`ratelimit.py` 里做，本文件只保证 I01 的验收 ③「5 次/15 分钟」可执行、可单测。
Redis 只存计数器，绝不入队（架构约束）。
"""

from __future__ import annotations

from typing import Protocol

from app.core.redis import cache_delete, cache_get, incr_with_ttl

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


class Throttle(Protocol):
    async def hit(self, key: str, ttl_seconds: int) -> int: ...

    async def get(self, key: str) -> int: ...

    async def reset(self, *keys: str) -> None: ...


class RedisThrottle:
    async def hit(self, key: str, ttl_seconds: int) -> int:
        return await incr_with_ttl(key, ttl_seconds)

    async def get(self, key: str) -> int:
        raw = await cache_get(key)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    async def reset(self, *keys: str) -> None:
        await cache_delete(*keys)


class MemoryThrottle:
    """进程内计数（单测用）。语义与 Redis 实现一致：hit 自增，reset 清零。"""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    async def hit(self, key: str, ttl_seconds: int) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def get(self, key: str) -> int:
        return self.counters.get(key, 0)

    async def reset(self, *keys: str) -> None:
        for key in keys:
            self.counters.pop(key, None)


def failure_keys(email: str, client_ip: str | None) -> tuple[str, str]:
    """双维度计数：同一 IP 爆破多账号、或同一账号被多地爆破，都要能拦住。"""
    normalized = email.strip().lower()
    return f"auth:login:fail:email:{normalized}", f"auth:login:fail:ip:{client_ip or 'unknown'}"
