"""intake 模块 ORM 模型。

只描述数据形状，业务规则在 service.py，HTTP 在 router.py。
单表多状态：raw / structured / promoted / archived_interest 全部走 status 列。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric, String

# 注意：本类有一个 `text` 列，会遮蔽 sqlalchemy.text；因此这里显式别名导入，
# 否则 `server_default=text("now()")` 会命中映射后的 MappedColumn 而报 TypeError。
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class IntakeItem(Base):
    """收件箱条目。一条一句话体感，可能演化为目标、兴趣或被丢弃。"""

    __tablename__ = "intake_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="raw", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    structured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    structured: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    structure_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    structure_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    promoted_goal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        # 5 分钟去重：text_hash + owner 维度查最近记录
        # 这里只建普通索引；查询用 created_at 范围 + text_hash 等值
    )


__all__ = ["IntakeItem"]
