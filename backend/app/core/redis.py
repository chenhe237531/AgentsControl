"""Redis 客户端（限流 / 短期缓存 / 分布式锁）。

只做这三件事，**绝不当队列**（队列在 Postgres，避免两套真相）。
所有写入型调用必须带 TTL。
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger("afc.redis")

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def ping_redis(timeout: float = 1.0) -> tuple[bool, str]:
    import asyncio

    try:
        async with asyncio.timeout(timeout):
            await get_redis().ping()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis ping failed", extra={"err": str(exc)})
        return False, type(exc).__name__


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        raise ValueError("缓存必须设置正数 TTL")
    await get_redis().set(key, value, ex=ttl_seconds)


async def cache_get(key: str) -> str | None:
    return await get_redis().get(key)


async def cache_delete(*keys: str) -> None:
    if keys:
        await get_redis().delete(*keys)


async def incr_with_ttl(key: str, ttl_seconds: int) -> int:
    """限流计数器：INCR + 首次设置 TTL，避免 Key 永久驻留。"""
    pipe = get_redis().pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl_seconds, nx=True)
    count, _ = await pipe.execute()
    return int(count)


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
