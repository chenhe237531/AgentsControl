"""RBAC：角色 → scope 映射表 + `require_scope()` 的实际接线。

设计约束（改之前先读）
----------------------
1. **scope 白名单只在本文件出现一次**。其它模块不要在 router 里硬编码字符串常量，
   统一 `from app.modules.identity.rbac import require_scope`。
2. 角色只有三档：`owner` / `operator` / `viewer`。未知角色 → 无任何 scope（默认拒绝）。
3. `x:write` **隐含** `x:read`：写权限一定包含读，避免给每个写端点再挂一遍读 scope。
4. 有效权限 = `ROLE_SCOPES[role]` ∪ 令牌里的显式 scopes（后者是**追加**，不是收窄）。
   要收窄权限就降角色，不要靠裁 scope 列表——否则「令牌里的快照」和「库里角色」
   会出现两个真相，越权排查会变成猜谜。
5. 越权一律 `403 + error.code=forbidden`（`core.errors.ForbiddenError` 已保证），
   **不返回 404**：本系统是单用户白名单，端点存在与否不是秘密，返回 404 只会让排查更难。

为什么不用 `core.deps.require_scope`：`core/` 是冻结文件，需要 x-shared-registry 流程；
且那里的 `Principal.has()` 只要 `role == "owner"` 就放行，无法表达三档角色与「写隐含读」。
本文件的 `require_scope` 与它签名兼容（都是 `require_scope(scope) -> 依赖函数`），
接线时可以无缝替换。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated

from fastapi import Depends

from app.core.deps import Principal, get_current_owner
from app.core.errors import ForbiddenError

# ---------------------------------------------------------------- scope 白名单
# 命名规则：`<资源>:<read|write>`。新增资源时**两边都要加**（这里 + ROLE_SCOPES），
# 漏加会让 `audit_scopes.py` 报 unknown_scope。
READ_SCOPES: tuple[str, ...] = (
    "auth:read",
    "intake:read",
    "goals:read",
    "tasks:read",
    "devices:read",
    "execution:read",
    "ledger:read",
    "approvals:read",
    "evals:read",
    "github:read",
)

WRITE_SCOPES: tuple[str, ...] = (
    "auth:write",
    "intake:write",
    "goals:write",
    "tasks:write",
    "devices:write",
    "execution:write",
    "ledger:write",
    "approvals:write",
    "evals:write",
    "github:write",
)

#: 会话类能力：与角色无关，三档角色都有——「登出自己」不该需要管理员权限。
SESSION_SCOPES: tuple[str, ...] = ("auth:session",)

ALL_SCOPES: tuple[str, ...] = tuple(sorted(READ_SCOPES + WRITE_SCOPES + SESSION_SCOPES))

#: owner 用通配符，不展开成具体列表：新增 scope 时不会因为忘记登记 owner 而锁死自己。
WILDCARD = "*"

_OPERATOR_WRITE: frozenset[str] = frozenset(
    {
        "intake:write",
        "goals:write",
        "tasks:write",
        "devices:write",
        "execution:write",
        "github:write",
    }
)
#: operator 刻意**没有** approvals:write / ledger:write / evals:write / auth:write：
#: 审批、记账、评测规则属于 owner 的决定权，执行者不能自己给自己批预算。
_OPERATOR_DENIED: frozenset[str] = frozenset(
    {"approvals:write", "ledger:write", "evals:write", "auth:write"}
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "owner": frozenset({WILDCARD}),
    "operator": frozenset(
        set(READ_SCOPES) | set(_OPERATOR_WRITE) | set(SESSION_SCOPES)
    ),
    "viewer": frozenset(set(READ_SCOPES) | set(SESSION_SCOPES)),
}

ROLES: tuple[str, ...] = ("owner", "operator", "viewer")

#: 登录/刷新天生无主体可依，必须公开；`/health|/ready|/version` 是 k8s 探活与前端版本探测，
#: 不返回任何业务数据。除此之外**没有**第二个豁免口子。
#: 审计脚本按 `(method, path)` 精确匹配，不做前缀通配，避免「一豁免就漏一片」。
PUBLIC_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/refresh"),
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/version"),
    }
)


# ---------------------------------------------------------------- 判定
def scopes_for_role(role: str) -> frozenset[str]:
    """角色默认 scope。未知角色返回空集（默认拒绝，不要抛异常——让调用方统一 403）。"""
    return ROLE_SCOPES.get(role, frozenset())


def expand(scopes: Iterable[str]) -> frozenset[str]:
    """展开通配符与「写隐含读」。"""
    given = {s for s in scopes if s}
    if WILDCARD in given:
        return frozenset(ALL_SCOPES)
    expanded = set(given)
    for scope in given:
        if scope.endswith(":write"):
            expanded.add(scope[: -len(":write")] + ":read")
    return frozenset(expanded)


def effective_scopes(role: str, scopes: Iterable[str] = ()) -> frozenset[str]:
    return expand(scopes_for_role(role) | set(scopes))


def has_scope(role: str, scopes: Iterable[str], required: str) -> bool:
    return required in effective_scopes(role, scopes)


def missing_scopes(role: str, scopes: Iterable[str], required: Iterable[str]) -> list[str]:
    effective = effective_scopes(role, scopes)
    return [s for s in required if s not in effective]


def check_scope(principal: Principal, *required: str) -> None:
    """判定失败抛 403（`forbidden`），成功静默返回。供 service 层直接调用。"""
    missing = missing_scopes(principal.role, principal.scopes, required)
    if missing:
        raise ForbiddenError(
            f"缺少权限: {', '.join(missing)}",
            details={"role": principal.role, "required_scopes": list(missing)},
        )


# ---------------------------------------------------------------- 依赖工厂
def require_scope(*required: str) -> Callable[..., object]:
    """生成一个 FastAPI 依赖：要求当前主体同时具备 `required` 里的全部 scope。

    用法与 `core.deps.require_scope` 一致：

        @router.post("/tasks")
        async def create_task(_: Annotated[Principal, Depends(require_scope("tasks:write"))]):
            ...

    `__rbac_scopes__` / `__rbac_auth__` 两个属性是**给审计脚本看的**：它按依赖树
    （而不是按源码正则）判断端点有没有挂认证/scope，所以只要依赖被正常挂上就一定扫得到。
    """
    wanted = tuple(required)
    if not wanted:
        raise ValueError("require_scope() 至少需要一个 scope")

    async def _dep(principal: Annotated[Principal, Depends(get_current_owner)]) -> Principal:
        check_scope(principal, *wanted)
        return principal

    _dep.__name__ = f"require_scope({'|'.join(wanted)})"
    _dep.__qualname__ = _dep.__name__
    # 审计脚本读取的两个标记（不是安全边界，别在运行时依赖它们）
    _dep.__rbac_scopes__ = wanted  # type: ignore[attr-defined]
    _dep.__rbac_auth__ = True  # type: ignore[attr-defined]
    return _dep


def role_matrix() -> dict[str, list[str]]:
    """给 `GET /auth/scopes` 用：每档角色的**展开后** scope 列表（可直接驱动前端按钮显隐）。"""
    return {role: sorted(expand(scopes_for_role(role))) for role in ROLES}
