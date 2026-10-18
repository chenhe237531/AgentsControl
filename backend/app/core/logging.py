"""结构化 JSON 日志 + 请求 ID 贯穿。

- 每条日志一行 JSON，便于 Loki/CloudWatch 直接检索；
- request_id / task_id / node_id 通过 contextvar 自动附加，不用逐处传参；
- 敏感字段（password/token/secret/authorization）在 formatter 层脱敏，防止误打。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from .config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
task_id_var: ContextVar[str] = ContextVar("task_id", default="")
node_id_var: ContextVar[str] = ContextVar("node_id", default="")

_SENSITIVE = ("password", "secret", "token", "authorization", "cookie", "api_key", "apikey")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "...[truncated]"
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in k.lower() for s in _SENSITIVE) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = _scrub(value)
        for name, var in (("request_id", request_id_var), ("task_id", task_id_var), ("node_id", node_id_var)):
            if var.get() and name not in entry:
                entry[name] = var.get()
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
