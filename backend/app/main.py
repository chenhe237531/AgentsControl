"""应用入口。

中间件添加顺序（Starlette 后加的在更外层）：
  CORS → SecurityHeaders → RequestContext
最终请求路径上的执行顺序：RequestContext → SecurityHeaders → CORS → 路由
即：先有 request_id，再谈其它。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import health_router
from app.api.router import api_router
from app.core.config import settings
from app.core.db import dispose_engine
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.redis import close_redis

logger = logging.getLogger("afc.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info(
        "api starting",
        extra={
            "env": settings.app_env,
            "version": settings.app_version,
            "db_pool": settings.db_pool_size,
        },
    )
    # 配置在 import 期已完成校验（fail fast）；此处只做资源预热，不阻塞启动。
    yield
    logger.info("api shutting down")
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Fleet Control API",
        version=settings.app_version,
        description="控制面 API：任务、预算、验收、账本。契约见 docs/DEV-PLAN-v1.md 与 /openapi.json。",
        docs_url=None if settings.app_env == "prod" else "/docs",
        redoc_url=None if settings.app_env == "prod" else "/redoc",
        openapi_url=None if settings.app_env == "prod" else "/openapi.json",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-Task-Id", "X-Node-Id"],
        expose_headers=["X-Request-Id", "Server-Timing"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
