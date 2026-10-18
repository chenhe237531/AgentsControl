"""HTTP 中间件。

顺序（由外到内，add_middleware 是逆序包装，故在 main.py 中后加的更靠外）：
AccessLog → SecurityHeaders → CORS → RequestContext(请求 ID) → 路由

说明：请求 ID 必须在最外层生成，才能覆盖异常处理器里打出的日志。
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging import new_request_id, node_id_var, request_id_var, task_id_var

access_logger = logging.getLogger("afc.access")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or new_request_id()
        request.state.request_id = rid
        tokens = [
            request_id_var.set(rid),
            task_id_var.set(request.headers.get("x-task-id", "")),
            node_id_var.set(request.headers.get("x-node-id", "")),
        ]
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            for cvar, token in zip((request_id_var, task_id_var, node_id_var), tokens):
                cvar.reset(token)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["x-request-id"] = rid
        response.headers["server-timing"] = f"app;dur={elapsed_ms}"
        if request.url.path not in ("/health", "/ready"):
            access_logger.info(
                "%s %s %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={"status": response.status_code, "ms": elapsed_ms},
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """生产由 Caddy 再加一层；应用层保留，防止绕过反代直连。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
