"""执行面的内部数据视图（Worker 协议的对外契约在 protocol.py）。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


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


# ---------------------------------------------------------------- 会话（W06-I03 起）

IssueRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
SlugRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]

_SLUG_RE = re.compile(SlugRef.__metadata__[0].pattern)


class AcceptanceSpec(BaseModel):
    """验收命令与硬性清单（任务自描述的一部分，随 .fleet-task.json 落 worktree）。"""

    model_config = ConfigDict(extra="ignore")

    cmd: str | None = Field(default=None, max_length=512)
    must_pass: list[str] = Field(default_factory=list, max_length=20)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(min_length=1, max_length=128)
    issue: IssueRef
    slug: SlugRef | None = None  # 缺省用 task_id；不能作 slug 时 422，要求显式传
    runtime: Literal["generic", "orca"] = "generic"
    base_ref: str | None = Field(default=None, max_length=256)
    device_id: str | None = Field(default=None, max_length=128)
    objective_id: str | None = Field(default=None, max_length=128)
    capability: str | None = Field(default=None, max_length=128)
    prompt_ver: str | None = Field(default=None, max_length=32)
    skill_versions: dict[str, str] | None = None
    budget_remaining: dict[str, Any] | None = None
    acceptance: AcceptanceSpec | None = None

    @model_validator(mode="after")
    def _default_slug(self) -> SessionCreateRequest:
        """slug 决定目录与分支名，字符集必须收紧；task_id 不合字符集时要求显式传 slug。"""
        if self.slug is None:
            if not _SLUG_RE.match(self.task_id):
                raise ValueError(
                    "task_id 不满足 slug 字符集（小写字母数字与连字符），请显式传 slug"
                )
            self.slug = self.task_id
        return self


class SessionView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    task_id: str
    issue: str
    slug: str
    runtime: str
    state: str
    branch: str
    worktree_path: str
    base_ref: str | None = None
    orca_session: str | None = None
    device_id: str | None = None
    objective_id: str | None = None
    transcript_url: str | None = None
    diff_summary: dict[str, Any] | None = None
    error: str | None = None
    fleet_task: dict[str, Any] = Field(default_factory=dict)
    spawned_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
