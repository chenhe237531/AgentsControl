"""Worker 协议 —— 执行面与一切运行时之间的唯一契约。

对应 `PRODUCT-DESIGN-v0.1.md` §3.3：任何 Worker（WorkBuddy / Trae / Opencode / 云手机 /
自建进程）只需实现 4 个 HTTP 接口即可接入舰队：

    POST /execution/workers/claim            领活（能力标签、并发余量、机型 → task + lease）
    POST /execution/workers/{id}/heartbeat   续约（task_id、进度、当前步骤）
    POST /execution/workers/{id}/complete    交货（产物引用、成本、步骤级 trace、耗时）
    POST /execution/workers/{id}/fail        报错（错误类型 retryable/不可重试、上下文）

设计纪律（见 plan/W06.md §2 红线）：
- claim 内部只调用 `tasks.service` 的公开函数（lease），**绝不直接写 tasks 表**；
- tasks 模块（W03）交付前的空档期里，四个接口统一返回 502 `tasks_unavailable`，不猜。

对 W03 的实际契约（已按 main 上 tasks.service 公开函数对齐）：
    tasks.service.claim(session, body: ClaimIn) -> tuple[Task, int] | None
        # ClaimIn = {device_id, caps: dict[str, bool], lease_seconds?}；caps 按 dict 传
    tasks.service.heartbeat(session, task_id: UUID, *, lease_token: UUID) -> Task
    tasks.service.complete(session, task_id: UUID, *, lease_token: UUID, result: dict) -> Task
    tasks.service.fail(session, task_id: UUID, *, lease_token: UUID,
                       error: str, retryable: bool) -> Task
    # 无 release：释放语义由 fail / 回收（reclaim）承担；网关的 release 为本地空操作

W03 的租约栅栏靠 lease_token（每次领取重新生成），本网关在 claim 时记录
task_id → lease_token 映射，续约/交货/失败自动携带；进程重启后映射丢失，
续约会以 lease_lost 冲突被拒（由启动清扫与回收路径兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.errors import ConflictError, UpstreamError

# ---------------------------------------------------------------- 请求 / 响应模型

ShortStr = Annotated[str, StringConstraints(min_length=1, max_length=64)]
CapTag = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class WorkerClaimRequest(BaseModel):
    """Worker 领活请求。对未知字段宽容（Worker 可附带调试信息），但对数值强校验。"""

    model_config = ConfigDict(extra="ignore")

    worker_id: ShortStr | None = None
    capabilities: list[CapTag] = Field(default_factory=list, max_length=50)
    concurrency_slots: int = Field(default=1, ge=1, le=256)
    machine: str | None = Field(default=None, max_length=128)


class TaskPayload(BaseModel):
    """派给 Worker 的任务体。payload 保持通用 dict：任务语义由 tasks 模块定义。"""

    model_config = ConfigDict(extra="ignore")

    task_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkerClaimResponse(BaseModel):
    worker_id: str
    task: TaskPayload
    lease_expires_at: datetime


class WorkerHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    progress: float | None = Field(default=None, ge=0.0, le=100.0)
    step: str | None = Field(default=None, max_length=256)


class WorkerHeartbeatResponse(BaseModel):
    task_id: str
    lease_expires_at: datetime


class ArtifactRef(BaseModel):
    """产物引用：库里只存引用，不存内容。"""

    name: str = Field(max_length=256)
    ref: str = Field(max_length=1024)  # cos://… / https://… / file://…
    bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, max_length=128)


class CostBreakdown(BaseModel):
    """成本明细。订阅制下 token 钱可为 0，但必须有结构（W07 按模型分项归集）。"""

    total: float = Field(ge=0.0)
    by_model: dict[str, float] = Field(default_factory=dict)


class TaskResult(BaseModel):
    """complete 必须携带 result：产物引用 + 成本（plan/W06.md I01 验收 ③）。"""

    model_config = ConfigDict(extra="ignore")

    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=100)
    cost: CostBreakdown
    elapsed_ms: int | None = Field(default=None, ge=0)
    steps: list[dict[str, Any]] | None = None  # 步骤级 trace，供 W07 归集


class WorkerCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    result: TaskResult


class WorkerCompleteResponse(BaseModel):
    task_id: str
    status: str = "completed"
    events_written: int


class WorkerFailRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    error_type: str = Field(max_length=100)
    retryable: bool
    context: dict[str, Any] | None = None


class WorkerFailResponse(BaseModel):
    task_id: str
    status: str = "failed"
    retryable: bool


# ---------------------------------------------------------------- 任务面网关（跨窗口只读依赖）

DEFAULT_LEASE_SECONDS = 90


class TasksUnavailableError(UpstreamError):
    """tasks 模块尚未交付（W03）或不可用。四类 Worker 接口此时统一 502，不降级猜测。"""

    code = "tasks_unavailable"
    message = "任务服务不可用：tasks 模块尚未交付或未部署（依赖 W03）"


@dataclass(frozen=True, slots=True)
class LeasedTask:
    task_id: str
    payload: dict[str, Any]
    lease_expires_at: datetime


@runtime_checkable
class TasksGateway(Protocol):
    """执行面对 tasks 模块的全部依赖（唯一入口是 tasks.service 的公开函数）。

    测试用 FakeTasksGateway 替换；生产用 TasksServiceGateway 动态加载 W03 的实现。
    """

    async def lease(
        self,
        *,
        capabilities: list[str],
        slots: int,
        machine: str | None,
        worker: str | None = None,
    ) -> LeasedTask | None: ...

    async def renew_lease(self, task_id: str, worker: str) -> datetime: ...

    async def release(self, task_id: str, worker: str) -> None: ...

    async def complete(self, task_id: str, worker: str, result: dict[str, Any]) -> None: ...

    async def fail(
        self,
        task_id: str,
        worker: str,
        error_type: str,
        retryable: bool,
        context: dict[str, Any] | None = None,
    ) -> None: ...


# task_id(str) → lease_token(str)：W03 的租约栅栏凭证，claim 时记录、终态时清除。
# 进程级映射——网关实例随请求创建，keeper 持有的也是请求期实例，映射必须放模块级。
_lease_tokens: dict[str, str] = {}


def _open_session() -> Any:
    """开一个独立 DB 会话（gateway 的调用方可能在后台任务里，没有请求级 session）。

    独立小函数是为了给契约测试一个替换点（monkeypatch 本函数即可，无需真 DB）。
    """
    from app.core.db import get_sessionmaker  # noqa: PLC0415

    return get_sessionmaker()()


def _task_uuid(task_id: str) -> Any:
    import uuid as _uuid  # noqa: PLC0415

    try:
        return _uuid.UUID(task_id)
    except ValueError as exc:
        raise TasksUnavailableError(f"task_id 不是合法 UUID：{task_id!r}") from exc


def _leased_from_claim(claimed: Any) -> LeasedTask:
    """把 tasks.service.claim 的返回 (Task, lease_seconds) 规整为 LeasedTask。

    解析失败抛 TasksUnavailableError——宁可拒绝也不把脏数据带进事件表。
    """
    task = claimed[0] if isinstance(claimed, tuple) else claimed
    try:
        task_id = str(task.id)
        payload = dict(task.payload or {})
    except AttributeError as exc:
        raise TasksUnavailableError(
            f"tasks.service.claim 返回了无法解析的任务（{type(task).__name__}）"
        ) from exc
    expires = getattr(task, "leased_until", None)
    if not isinstance(expires, datetime):
        expires = datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    token = getattr(task, "lease_token", None)
    if token is not None:
        _lease_tokens[task_id] = str(token)
    return LeasedTask(task_id=str(task.id), payload=payload, lease_expires_at=expires)


def _lease_token_of(task_id: str) -> Any:
    """取租约栅栏 token；没有（跨进程/重启后首次续约）按 W03 语义拒绝。"""
    import uuid as _uuid  # noqa: PLC0415

    token = _lease_tokens.get(task_id)
    if not token:
        raise ConflictError(
            f"租约凭证缺失（进程重启或未在本进程领取）：task={task_id}",
            code="lease_lost",
        )
    return _uuid.UUID(token)


class TasksServiceGateway:
    """生产实现：按 main 上 tasks.service 的真实公开 API 调用。

    - 每个方法自开 DB 会话（调用方可能是请求线程，也可能是 keeper 后台任务）；
    - 续约走 heartbeat(session, task_id, lease_token=...)；
    - 领活走 claim(session, ClaimIn)，device_id 取 worker/machine（W03 的公平性
      统计按 device 维度，worker 身份稳定且语义正确）；
    - W03 无 release：本网关的 release 是空操作，靠租约过期 + reclaim 兜底。

    tasks 模块不可用时统一抛 TasksUnavailableError（HTTP 502）。
    """

    _REQUIRED = ("claim", "heartbeat", "complete", "fail")

    def _service(self) -> Any:
        try:
            from app.modules.tasks import service as tasks_service  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - 任何导入失败都归因为「不可用」
            raise TasksUnavailableError(f"tasks.service 加载失败：{type(exc).__name__}") from exc
        missing = [name for name in self._REQUIRED if not callable(getattr(tasks_service, name, None))]
        if missing:
            raise TasksUnavailableError(f"tasks.service 缺少公开函数 {missing}（W03 未交付）")
        return tasks_service

    def _claim_in(self) -> Any:
        from app.modules.tasks.schemas import ClaimIn  # noqa: PLC0415

        return ClaimIn

    async def lease(
        self,
        *,
        capabilities: list[str],
        slots: int,
        machine: str | None,
        worker: str | None = None,
    ) -> LeasedTask | None:
        ClaimIn = self._claim_in()
        body = ClaimIn(
            device_id=worker or machine or "execution-worker",
            caps={cap: True for cap in capabilities},
        )
        async with _open_session() as session:
            claimed = await self._service().claim(session, body)
            if claimed is None:
                return None
            return _leased_from_claim(claimed)

    async def renew_lease(self, task_id: str, worker: str) -> datetime:
        task_uuid = _task_uuid(task_id)
        token = _lease_token_of(task_id)
        async with _open_session() as session:
            task = await self._service().heartbeat(session, task_uuid, lease_token=token)
        expires = getattr(task, "leased_until", None)
        if not isinstance(expires, datetime):
            expires = datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)
        return expires

    async def release(self, task_id: str, worker: str) -> None:
        # W03 无 release 公开函数：交还给回收器（租约到期 → reclaim 重投/死信）。
        return None

    async def complete(self, task_id: str, worker: str, result: dict[str, Any]) -> None:
        task_uuid = _task_uuid(task_id)
        token = _lease_token_of(task_id)
        async with _open_session() as session:
            await self._service().complete(
                session, task_uuid, lease_token=token, result=result
            )
        _lease_tokens.pop(task_id, None)

    async def fail(
        self,
        task_id: str,
        worker: str,
        error_type: str,
        retryable: bool,
        context: dict[str, Any] | None = None,
    ) -> None:
        task_uuid = _task_uuid(task_id)
        token = _lease_token_of(task_id)
        error = error_type if not context else f"{error_type}: {context}"[:500]
        async with _open_session() as session:
            await self._service().fail(
                session, task_uuid, lease_token=token, error=error, retryable=retryable
            )
        _lease_tokens.pop(task_id, None)
