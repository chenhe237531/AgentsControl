"""设备接入业务逻辑（I01：注册与 token；I03–I06 逐步扩展心跳/事件/快照/指令）。

分层约定：
- 不依赖 Request/Response，只操作领域对象与 Pydantic 模型；
- 多步写入放在一个事务里（末尾统一 `commit()`）；
- 状态机与时钟相关计算全部经过注入的 `clock`，测试可控、不 sleep。

三通道语义（TECH-ARCHITECTURE §0.2 / §4.3，任何一条做错都等于设备已死）：
- 心跳 60s：丢 3 次 → suspect（停止派新活）；丢 5 分钟 → offline（回收租约，只发事件）；
- 增量 30s 批量 + 游标：幂等基础是 (node_id, seq) 唯一索引；
- 快照 60min：收到即标记 woke_at，早于快照的增量不打乱（is_backfill）。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.core.security import hash_device_token

from .repository import DeviceRecord, DeviceRepository
from .schemas import TRUST_VALUES, RegisterIn

#: 引导码默认有效期（人工签发，一次性使用）
BOOTSTRAP_CODE_TTL_DAYS = 7
_ALLOWED_PATCH_FIELDS = ("caps", "tolerance", "squad", "purpose", "trust")


@dataclass(slots=True)
class RegisterResult:
    node_id: str
    token: str | None
    created: bool
    reactivated: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeviceService:
    def __init__(
        self, repo: DeviceRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._repo = repo
        self._clock = clock or _utcnow

    # ------------------------------------------------------------- I01 注册与 token

    async def issue_bootstrap_code(self, *, ttl_days: int = BOOTSTRAP_CODE_TTL_DAYS) -> tuple[str, datetime | None]:
        """人工签发引导码：明文只在响应中出现一次，库里只存 sha256。"""
        raw = secrets.token_urlsafe(24)
        expires_at = self._clock() + timedelta(days=ttl_days) if ttl_days > 0 else None
        await self._repo.create_bootstrap_code(hash_device_token(raw), expires_at)
        await self._repo.commit()
        return raw, expires_at

    async def register(self, payload: RegisterIn) -> RegisterResult:
        """注册。

        - 必须持有效引导码（一次性、未过期）——不允许无凭据自助注册；
        - (指纹, node_id) 已存在且未吊销 → 幂等返回既有节点，不签发新 token；
        - 指纹已绑定其它 node_id → 409（防止一台机器伪装成两个节点）；
        - 已吊销的设备凭新引导码重注册 → 重激活并换发新 token（旧 token 已被清空）。
        """
        now = self._clock()
        code_hash = hash_device_token(payload.bootstrap_code)
        if not await self._repo.bootstrap_code_valid(code_hash, now=now):
            raise UnauthorizedError("引导码无效、已使用或已过期")

        existing = await self._repo.get_device_by_fingerprint(payload.fingerprint)
        if existing is not None:
            if existing.node_id != payload.node_id:
                raise ConflictError("该机器指纹已绑定其它节点")
            if existing.status == "revoked":
                # 吊销后重注册：换发新 token，重新上线
                token = secrets.token_urlsafe(32)
                await self._repo.update_device(
                    existing.node_id,
                    token_hash=hash_device_token(token),
                    status="offline",
                    revoked_at=None,
                    missed_heartbeats=0,
                )
                await self._repo.mark_bootstrap_used(code_hash, payload.node_id, now=now)
                await self._repo.commit()
                return RegisterResult(payload.node_id, token, created=False, reactivated=True)
            # 幂等：同指纹同 node_id 重复注册，返回既有节点，不消耗引导码
            return RegisterResult(existing.node_id, None, created=False, reactivated=False)

        clash = await self._repo.get_device(payload.node_id)
        if clash is not None:
            raise ConflictError("node_id 已被其它机器占用")

        token = secrets.token_urlsafe(32)
        record = DeviceRecord(
            node_id=payload.node_id,
            fingerprint=payload.fingerprint,
            token_hash=hash_device_token(token),
            trust=payload.trust,
            purpose=payload.purpose,
            squad=payload.squad,
            tolerance=payload.tolerance,
            caps=list(payload.caps),
        )
        await self._repo.create_device(record)
        await self._repo.mark_bootstrap_used(code_hash, payload.node_id, now=now)
        await self._repo.commit()
        return RegisterResult(payload.node_id, token, created=True, reactivated=False)

    async def list_devices(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[DeviceRecord]:
        if status is not None and status not in ("online", "suspect", "offline", "revoked"):
            raise ValidationError("status 取值非法")
        return await self._repo.list_devices(status=status, limit=limit, offset=offset)

    async def update_device(self, node_id: str, patch: dict) -> DeviceRecord:
        device = await self._require_device(node_id)
        updates = {k: v for k, v in patch.items() if k in _ALLOWED_PATCH_FIELDS and v is not None}
        if not updates:
            return device
        if "trust" in updates and updates["trust"] not in TRUST_VALUES:
            raise ValidationError("trust 取值非法")
        updated = await self._repo.update_device(node_id, **updates)
        assert updated is not None
        await self._repo.commit()
        return updated

    async def revoke_device(self, node_id: str) -> DeviceRecord:
        """吊销设备：token_hash 置空即刻失效（HMAC 验签拿不到密钥），状态转 revoked。"""
        await self._require_device(node_id)
        updated = await self._repo.update_device(
            node_id, status="revoked", revoked_at=self._clock(), token_hash=None
        )
        assert updated is not None
        await self._repo.commit()
        return updated

    async def get_device(self, node_id: str) -> DeviceRecord:
        return await self._require_device(node_id)

    async def get_device_for_auth(self, node_id: str) -> DeviceRecord | None:
        """供 HMAC 验签依赖使用：不抛错，把"存在性"折叠进签名校验防枚举。"""
        return await self._repo.get_device(node_id)

    async def _require_device(self, node_id: str) -> DeviceRecord:
        device = await self._repo.get_device(node_id)
        if device is None:
            raise NotFoundError("设备不存在")
        return device

    def _require_not_third_party(self, device: DeviceRecord, action: str) -> None:
        if device.trust == "third_party":
            raise ForbiddenError(f"第三方设备不接收{action}")
