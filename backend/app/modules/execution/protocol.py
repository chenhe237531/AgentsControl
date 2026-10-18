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

对 W03 的契约期望（写在 handoff 里，由 W03 在其计划中排期）：
    tasks.service.lease(*, capabilities: list[str], slots: int, machine: str | None)
        -> {"task_id": str, "payload": dict, "lease_expires_at": datetime} | None
    tasks.service.renew_lease(task_id: str, worker: str) -> datetime
    tasks.service.complete(task_id: str, worker: str, result: dict) -> None
    tasks.service.fail(task_id: str, worker: str, error_type: str,
                       retryable: bool, context: dict | None) -> None
    tasks.service.release(task_id: str, worker: str) -> None
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.errors import UpstreamError

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
        self, *, capabilities: list[str], slots: int, machine: str | None
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


def _coerce_leased(raw: Any) -> LeasedTask:
    """把 tasks.service.lease 的返回值规整为 LeasedTask。

    允许 dict 或带属性的对象（W03 的具体返回形态在 Wave 边界对齐，这里两般都认）。
    解析失败抛 TasksUnavailableError——宁可拒绝也不把脏数据带进事件表。
    """
    expires: datetime | None
    task_id: str | None
    payload: dict[str, Any] = {}
    if isinstance(raw, dict):
        task_id = raw.get("task_id")
        payload = dict(raw.get("payload") or {})
        expires = raw.get("lease_expires_at")
    else:
        task_id = getattr(raw, "task_id", None)
        payload = dict(getattr(raw, "payload", None) or {})
        expires = getattr(raw, "lease_expires_at", None)
    if not task_id:
        raise TasksUnavailableError("tasks.service.lease 返回了无法解析的任务（缺少 task_id）")
    if not isinstance(expires, datetime):
        expires = datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    return LeasedTask(task_id=str(task_id), payload=payload, lease_expires_at=expires)


class TasksServiceGateway:
    """生产实现：延迟加载 `app.modules.tasks.service`，按公开函数契约调用。

    W03 未交付时（ImportError / 缺函数 / lease 不可用），统一抛 TasksUnavailableError，
    由全局错误处理器转成 502 `tasks_unavailable`。
    """

    _REQUIRED = ("lease", "renew_lease", "complete", "fail")

    def _service(self) -> Any:
        try:
            from app.modules.tasks import service as tasks_service  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 - 任何导入失败都归因为「不可用」
            raise TasksUnavailableError(f"tasks.service 加载失败：{type(exc).__name__}") from exc
        missing = [name for name in self._REQUIRED if not callable(getattr(tasks_service, name, None))]
        if missing:
            raise TasksUnavailableError(f"tasks.service 缺少公开函数 {missing}（W03 未交付）")
        return tasks_service

    async def lease(
        self, *, capabilities: list[str], slots: int, machine: str | None
    ) -> LeasedTask | None:
        raw = await self._service().lease(
            capabilities=capabilities, slots=slots, machine=machine
        )
        return None if raw is None else _coerce_leased(raw)

    async def renew_lease(self, task_id: str, worker: str) -> datetime:
        expires = await self._service().renew_lease(task_id=task_id, worker=worker)
        if not isinstance(expires, datetime):
            expires = datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)
        return expires

    async def release(self, task_id: str, worker: str) -> None:
        release = getattr(self._service(), "release", None)
        if callable(release):  # release 是可选函数：没有就靠 renew/fail 语义兜底
            await release(task_id=task_id, worker=worker)

    async def complete(self, task_id: str, worker: str, result: dict[str, Any]) -> None:
        await self._service().complete(task_id=task_id, worker=worker, result=result)

    async def fail(
        self,
        task_id: str,
        worker: str,
        error_type: str,
        retryable: bool,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self._service().fail(
            task_id=task_id, worker=worker, error_type=error_type,
            retryable=retryable, context=context,
        )
