"""健康检查与版本端点。

liveness 与 readiness 必须分开：
- `/health`：进程活着就 200，不受依赖影响（否则 K8s/Compose 会误杀容器）
- `/ready`：DB 必须通；Redis 不通只降级不致命（限流退化但核心可用）
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.core.db import ping_database
from app.core.redis import ping_redis

health_router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


@health_router.get("/health", summary="liveness")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
        "uptime_s": round(time.time() - _STARTED_AT, 1),
    }


@health_router.get("/ready", summary="readiness（含依赖探测）")
async def ready(response: Response) -> dict[str, object]:
    db_ok, db_detail = await ping_database()
    redis_ok, redis_detail = await ping_redis()

    overall = "ok" if db_ok else "down"
    if db_ok and not redis_ok:
        overall = "degraded"
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": overall,
        "checks": {
            "database": {"ok": db_ok, "detail": db_detail},
            "redis": {"ok": redis_ok, "detail": redis_detail},
        },
        "git_sha": os.getenv("GIT_SHA", "unknown"),
    }


@health_router.get("/version", summary="构建版本")
async def version() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "git_sha": os.getenv("GIT_SHA", "unknown"),
        "built_at": os.getenv("BUILT_AT", "unknown"),
    }
