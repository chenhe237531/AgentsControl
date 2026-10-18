"""执行面的内部数据视图（Worker 协议的对外契约在 protocol.py）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrcaHealthView(BaseModel):
    """GET /execution/orca/health 的响应：探测结果的只读视图。"""

    model_config = ConfigDict(extra="ignore")

    available: bool
    checked_at: datetime
    cli_path: str | None = None
    version: str | None = None
    agents: list[str] = Field(default_factory=list)
    worktree_root: str | None = None
    worktree_root_writable: bool = False
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
