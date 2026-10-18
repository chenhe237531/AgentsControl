"""类型化错误体系 + 全局错误处理。

约定：
- 客户端只看到 `{error: {code, message, request_id, details?}}`；
- 绝不把堆栈、SQL、内部路径返回给客户端；
- 每个错误都有稳定的 `code`，前端据此做文案映射（不解析 message）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("afc.error")


class AppError(Exception):
    """所有业务错误的基类。status_code 决定 HTTP 语义，code 决定前端行为。"""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)


class ValidationError(AppError):
    status_code, code, message = 422, "validation_failed", "参数校验失败"


class NotFoundError(AppError):
    status_code, code, message = 404, "not_found", "对象不存在"


class ConflictError(AppError):
    status_code, code, message = 409, "conflict", "状态冲突，请刷新后重试"


class UnauthorizedError(AppError):
    status_code, code, message = 401, "unauthorized", "未认证或凭据已过期"


class ForbiddenError(AppError):
    status_code, code, message = 403, "forbidden", "无权执行该操作"


class RateLimitedError(AppError):
    status_code, code, message = 429, "rate_limited", "请求过于频繁"


class UpstreamError(AppError):
    status_code, code, message = 502, "upstream_error", "上游服务不可用"


class NotImplementedYetError(AppError):
    """模块占位：窗口尚未实现该端点。便于并行开发期区分「没实现」与「坏了」。"""

    status_code, code, message = 501, "not_implemented", "该能力尚未实现"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _payload(code: str, message: str, request_id: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "request_id": request_id}}
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = _request_id(request)
        level = logging.WARNING if exc.status_code < 500 else logging.ERROR
        logger.log(level, exc.message, extra={"code": exc.code, "status": exc.status_code, "request_id": rid})
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, rid, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = _request_id(request)
        details = [
            {"loc": ".".join(str(p) for p in e.get("loc", [])), "msg": e.get("msg", "")}
            for e in exc.errors()[:10]
        ]
        return JSONResponse(
            status_code=422,
            content=_payload("validation_failed", "参数校验失败", rid, {"fields": details}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = _request_id(request)
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(status_code=exc.status_code, content=_payload(code, str(exc.detail), rid))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = _request_id(request)
        logger.exception("unhandled exception", extra={"request_id": rid, "path": request.url.path})
        return JSONResponse(
            status_code=500, content=_payload("internal_error", "服务内部错误", rid)
        )
