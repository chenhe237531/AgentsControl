"""设备接入模块路由 —— owner: W04

端点分两类认证：
- Owner（Bearer JWT，经 core.deps.OwnerDep）：注册引导码签发、设备管理、状态/事件/快照查询、指令下发；
- Device（HMAC 签名，经 hmac.verify_device_signature）：heartbeat / events / snapshot 上报、指令长轮询与 ack。

骨架期的 /_status、/_ping 保留：它们在 openapi.snapshot.json 契约快照中登记，
删除或改名属于契约删除（CI 会拒绝）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.deps import OwnerDep
from app.core.errors import ConflictError

from .deps import DeviceServiceDep
from .schemas import (
    BootstrapCodeOut,
    DeviceOut,
    DevicePatchIn,
    RegisterIn,
    RegisterOut,
    RevokeOut,
)

MODULE = "devices"
OWNER_WINDOW = "W04"

router = APIRouter(prefix="/devices", tags=["devices"])


# ---------------------------------------------------------------------------
# 骨架自检端点（契约已登记进 openapi.snapshot.json，保持不变）
# ---------------------------------------------------------------------------


@router.get("/_status", summary="模块占位状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "active"}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "active"}


# ---------------------------------------------------------------------------
# I01 · 注册与设备管理（owner 认证，除 register 凭引导码外）
# ---------------------------------------------------------------------------


@router.post("/register", response_model=RegisterOut, summary="设备注册（凭一次性引导码）")
async def register_device(payload: RegisterIn, service: DeviceServiceDep) -> RegisterOut:
    result = await service.register(payload)
    return RegisterOut(
        node_id=result.node_id,
        token=result.token,
        created=result.created,
        reactivated=result.reactivated,
    )


@router.post("/bootstrap-codes", response_model=BootstrapCodeOut, summary="人工签发注册引导码")
async def issue_bootstrap_code(_: OwnerDep, service: DeviceServiceDep) -> BootstrapCodeOut:
    code, expires_at = await service.issue_bootstrap_code()
    return BootstrapCodeOut(code=code, expires_at=expires_at)


@router.get("/", response_model=list[DeviceOut], summary="设备列表")
async def list_devices(
    _: OwnerDep,
    service: DeviceServiceDep,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DeviceOut]:
    records = await service.list_devices(status=status, limit=limit, offset=offset)
    return [DeviceOut.model_validate(r) for r in records]


@router.patch("/{node_id}", response_model=DeviceOut, summary="更新设备（caps/tolerance/squad/purpose/trust）")
async def patch_device(
    node_id: str, payload: DevicePatchIn, _: OwnerDep, service: DeviceServiceDep
) -> DeviceOut:
    record = await service.update_device(node_id, patch=payload.model_dump())
    return DeviceOut.model_validate(record)


@router.post("/{node_id}/revoke", response_model=RevokeOut, summary="吊销设备（token 立即失效）")
async def revoke_device(node_id: str, _: OwnerDep, service: DeviceServiceDep) -> RevokeOut:
    record = await service.revoke_device(node_id)
    if record.revoked_at is None:  # 理论不可达，防御类型检查
        raise ConflictError("吊销失败")
    return RevokeOut(node_id=record.node_id, status=record.status, revoked_at=record.revoked_at)
