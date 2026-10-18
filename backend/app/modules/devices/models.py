"""设备接入模块 ORM 模型 —— owner: W04

表与迁移的对应关系（迁移链：0001_baseline → w04_01 → w04_02 → w04_03 → w04_04）：
- w04_01_devices:   devices、device_bootstrap_codes、device_heartbeats（I01；heartbeats 供 I03 使用）
- w04_02_events:    device_events、device_alerts（I04）
- w04_03_snapshots: device_snapshots、device_cap_events（I05）
- w04_04_commands:  device_commands（I06）

硬约束（TECH-ARCHITECTURE §4.2/§4.3）：
- 幂等基础是 device_events 的 UNIQUE(node_id, seq)，不是应用层去重；
- 设备 token 只存 sha256（token_hash），明文不落库；
- 时钟以服务端 received_at 为准，设备时间只用于排序。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

#: 设备状态机取值（I03）
DEVICE_STATUSES = ("online", "suspect", "offline", "revoked")
#: 信任级别：third_party 只能上报数据，永不接收指令
TRUST_LEVELS = ("managed", "third_party")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # 设备自报名（与 agent 端 protocol.Heartbeat.node 一致），owner 控制台也用它寻址
    node_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # 机器指纹（agent 端汇总硬件特征生成）。注册幂等与冲突判定都以它为准
    fingerprint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # sha256(明文 token)。明文只在注册/重激活响应里出现一次。
    # HMAC 签名密钥 = 该哈希值（设备端对 raw token 做同样的 sha256 派生），吊销后置 NULL。
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    trust: Mapped[str] = mapped_column(String(16), nullable=False, default="managed",
                                       server_default="managed")
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    squad: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可容忍离线秒数（笔记本休眠类设备可以给大值）
    tolerance: Mapped[int] = mapped_column(Integer, nullable=False, default=300, server_default="300")
    # 能力标签（gui/orca/browser/container/always_on…），快照变更时联动更新
    caps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list,
                                       server_default=text("'[]'::jsonb"))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline",
                                        server_default="offline")
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missed_heartbeats: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                   server_default="0")
    # 增量事件游标（该节点已上报的最大 seq）
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0,
                                                server_default="0")
    # 唤醒标记：收到快照即刷新（唤醒后先快照再增量的容错依赖它）
    woke_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now(), onupdate=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceBootstrapCode(Base):
    """人工签发的引导码：没有它不允许自助注册。同样只存哈希。"""

    __tablename__ = "device_bootstrap_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_node: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
