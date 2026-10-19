"""资产面与模型雷达模块 —— owner: W08

20 道自有题 × N 模型、8 维得分、能力上限标记、漂移预警、skill/harness 版本与认知卡。

分层约定（不可越界）：
- `router.py`      只做 HTTP：解析入参、调用 service、格式化响应，**不写业务逻辑**
- `service.py`     业务规则与编排，**不依赖 Request/Response**，多步写入走事务
- `repository.py`  数据访问，只接受/返回领域对象或行记录
- `models.py`      ORM 模型，继承 `app.core.db.Base`

本文件是骨架占位：`/_status` 返回 501 供并行开发期自检。
窗口实现时删除 `/_status`，并按 PLAN 中的端点清单逐个落地。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import OwnerDep
from app.core.errors import NotImplementedYetError

MODULE = "evals"
OWNER_WINDOW = "W08"

router = APIRouter(prefix="/evals", tags=["evals"])


@router.get("/_status", summary="模块占位状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "stub"}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    raise NotImplementedYetError(f"模块 {MODULE} 尚未实现，owner={OWNER_WINDOW}")
