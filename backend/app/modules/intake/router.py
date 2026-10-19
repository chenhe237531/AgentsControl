"""intake HTTP 路由。

只做 HTTP：解析入参、调 service、格式化响应。业务规则在 service.py。
依赖注入：
- `get_intake_repository` 用 `get_db` 拿 AsyncSession 构造 Sql 实现；
- 测试可用 `app.dependency_overrides[get_intake_repository] = ...` 注入 fake。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.db import get_db
from app.core.deps import OwnerDep
from app.modules.intake import service as svc
from app.modules.intake.litellm import DEFAULT_MODEL, HttpLitellmClient, LitellmClient
from app.modules.intake.repository import IntakeRepository, SqlIntakeRepository
from app.modules.intake.schemas import (
    IntakeItemCreate,
    IntakeItemList,
    IntakeItemOut,
    StructureRequest,
    StructureResult,
)

MODULE = "intake"
OWNER_WINDOW = "W02"

router = APIRouter(prefix="/intake", tags=["intake"])


@router.get("/_status", summary="模块状态（骨架冒烟测试依赖；保留到统一清理波次）")
async def module_status() -> dict[str, str]:
    return {"module": MODULE, "owner_window": OWNER_WINDOW, "state": "implemented"}


async def get_intake_repository(
    session=Depends(get_db),
) -> IntakeRepository:
    return SqlIntakeRepository(session)


IntakeRepoDep = Annotated[IntakeRepository, Depends(get_intake_repository)]


def get_litellm_client() -> LitellmClient:
    """LiteLLM 客户端工厂。每个请求新建一个 httpx 客户端：

    - 简化生命周期管理（httpx 内部连接池本身就优）；
    - 测试用 dependency_overrides 注入 fake / MockTransport 客户端。
    """
    return HttpLitellmClient(model=DEFAULT_MODEL)


LitellmDep = Annotated[LitellmClient, Depends(get_litellm_client)]


@router.post(
    "/items",
    response_model=IntakeItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="一句话捕获（≤30 秒记录成本）",
)
async def create_item(
    payload: IntakeItemCreate,
    owner: OwnerDep,
    repo: IntakeRepoDep,
    response: Response,
) -> IntakeItemOut:
    out, created = await svc.capture(repo, owner.subject, payload)
    if not created:
        # 5 分钟内重复：返回既有条目，但用 200 而非 201
        response.status_code = status.HTTP_200_OK
    return out


@router.get("/items", response_model=IntakeItemList, summary="列出收件箱（keyset 分页）")
async def list_items(
    owner: OwnerDep,
    repo: IntakeRepoDep,
    cursor: str | None = Query(default=None, description="上一页返回的 next_cursor"),
    limit: int = Query(default=svc.DEFAULT_PAGE_SIZE, ge=1, le=svc.MAX_PAGE_SIZE),
) -> IntakeItemList:
    return await svc.list_items(repo, owner.subject, cursor=cursor, limit=limit)


@router.get(
    "/items/{item_id}",
    response_model=IntakeItemOut,
    summary="获取单条 intake",
)
async def get_item(owner: OwnerDep, repo: IntakeRepoDep, item_id: str) -> IntakeItemOut:
    return await svc.get_one(repo, owner.subject, item_id)


@router.get(
    "/interests",
    response_model=list[IntakeItemOut],
    summary="兴趣池（14 天未结构化的归档条目）",
)
async def list_interests(
    owner: OwnerDep,
    repo: IntakeRepoDep,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[IntakeItemOut]:
    return await svc.list_interests(repo, owner.subject, limit=limit)


# ---------------------------------------------------------------------------
# W02-I02 · AI 三问结构化
# ---------------------------------------------------------------------------


@router.post(
    "/items/{item_id}/structure",
    response_model=StructureResult,
    summary="AI 三问结构化（幂等）",
)
async def structure_item(
    owner: OwnerDep,
    repo: IntakeRepoDep,
    litellm: LitellmDep,
    item_id: str,
    _: StructureRequest = StructureRequest(),
) -> StructureResult:
    return await svc.structure(repo, litellm, owner.subject, item_id)


@router.get(
    "/items/{item_id}/questions",
    response_model=StructureResult,
    summary="获取该 item 的三问答案（未结构化返回 404）",
)
async def get_questions(
    owner: OwnerDep, repo: IntakeRepoDep, item_id: str
) -> StructureResult:
    return await svc.get_questions(repo, owner.subject, item_id)


__all__ = ["router"]
