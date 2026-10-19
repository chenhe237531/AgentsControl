"""intake 模块的 Pydantic 契约。

字段命名是稳定契约，前端据此生成类型化客户端，**不可随意重命名**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class IntakeItemCreate(BaseModel):
    """POST /intake/items 请求体。

    记录成本 ≤30 秒的硬约束：只要求 `text`，其余全部可选。
    """

    text: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(default="manual", max_length=32)


class IntakeItemOut(BaseModel):
    id: str
    owner_subject: str
    text: str
    source: str
    status: Literal["raw", "structured", "promoted", "archived_interest"]
    created_at: datetime
    structured_at: datetime | None = None
    structured: dict | None = None
    structure_model: str | None = None
    structure_cost_usd: float | None = None
    archived_at: datetime | None = None
    promoted_goal_id: str | None = None

    model_config = {"from_attributes": True}


class IntakeItemList(BaseModel):
    """keyset 分页响应。next_cursor 为 null 表示已到末尾。"""

    items: list[IntakeItemOut]
    next_cursor: str | None = None


class StructureRequest(BaseModel):
    """POST /intake/items/{id}/structure 可选体。

    留空以便未来传入偏好；当前不接受任何字段，幂等性由 service 保证。
    """

    pass


class ThreeQuestions(BaseModel):
    """AI 三问的固定字段名。**字段名不可改**（前端、迁移、文档都引用）。"""

    acceptance: str | None = Field(
        default=None,
        description="做完之后什么变了？答不出时应建议放入兴趣池，绝不编造",
    )
    horizon: str | None = Field(
        default=None, description="多久内想看到结果（TTL 表达，自然语言）"
    )
    budget: str | None = Field(
        default=None, description="愿意花多少（钱或注意力小时，自然语言）"
    )
    suggest_interest: bool = Field(
        default=False, description="AI 判定第一问答不出 → 建议放入兴趣池"
    )


class StructureResult(BaseModel):
    item_id: str
    structured: ThreeQuestions
    model: str
    cost_usd: float


__all__ = [
    "IntakeItemCreate",
    "IntakeItemOut",
    "IntakeItemList",
    "StructureRequest",
    "ThreeQuestions",
    "StructureResult",
]
