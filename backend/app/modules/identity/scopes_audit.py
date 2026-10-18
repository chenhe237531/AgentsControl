"""路由权限审计：把「哪个端点漏挂认证/scope」变成一次可断言的计算。

为什么放在 identity 模块而不是纯脚本里：`backend/scripts/audit_scopes.py` 只是 CLI 外壳，
真正的扫描逻辑必须能被单测覆盖（构造一个故意漏挂 scope 的假 App，断言它被抓出来）。
放在 `scripts/` 里的代码测不到，等于没测。

判定依据是**依赖树**而不是源码正则：
FastAPI 的 `APIRoute.dependant` 已经解析好了每个端点的依赖链，`rbac.require_scope()`
和 `core.deps.get_current_owner` 都在上面留了 `__rbac_scopes__` / `__rbac_auth__` 标记。
源码正则会被装饰器顺序、别名导入、跨行写法骗过去，依赖树不会。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from .rbac import ALL_SCOPES, PUBLIC_ENDPOINTS, ROLES

#: 只有这些方法算「写」。POST 的读语义端点（如 /search）仍会被要求挂 scope——
#: 这是**故意的**：宁可多挂一个 scope，也不要靠「这个 POST 其实只读」的口头约定。
WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SKIP_METHODS: frozenset[str] = frozenset({"HEAD", "OPTIONS"})

# 骨架自检端点：`/_status` 必须公开（CI 的 smoke job 与运维探活用），`_ping` 必须认证。
_PROBE_SUFFIXES: tuple[str, ...] = ("/_status",)

KIND_MISSING_AUTH = "missing_auth"
KIND_MISSING_SCOPE = "missing_scope"
KIND_UNKNOWN_SCOPE = "unknown_scope"
KIND_STALE_PUBLIC = "stale_public"


@dataclass(frozen=True, slots=True)
class Violation:
    kind: str
    method: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "method": self.method, "path": self.path, "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.kind:<14} {self.method:<6} {self.path}  {self.detail}"


@dataclass(slots=True)
class Report:
    total: int = 0
    public: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total_endpoints": self.total,
            "public_endpoints": list(self.public),
            "violations": [v.as_dict() for v in self.violations],
        }

    def format_text(self) -> str:
        if self.ok:
            return (
                f"OK  已扫描 {self.total} 个端点，全部挂载认证/scope"
                f"（公开端点 {len(self.public)} 个）"
            )
        lines = [f"扫描 {self.total} 个端点，发现 {len(self.violations)} 个问题："]
        lines += [f"  {v}" for v in self.violations]
        return "\n".join(lines)


def _iter_dependant(dependant: Any) -> Any:
    for sub in dependant.dependencies:
        yield sub
        yield from _iter_dependant(sub)


def _is_endpoint(obj: Any) -> bool:
    """鸭子类型判断「这是一个端点」：有 dependant + methods + path。

    不用 `isinstance(route, APIRoute)`：FastAPI ≥0.130 用 `_IncludedRouter` 做惰性 include，
    `app.routes` 里拿到的是 include 包装器而不是 `APIRoute`，硬类型判断会扫出 0 个端点
    （这个 bug 真的发生过，且脚本 exit 0——「什么都没扫到」是最危险的通过）。
    """
    return (
        hasattr(obj, "dependant")
        and getattr(obj, "methods", None) is not None
        and getattr(obj, "path", None) is not None
    )


def iter_endpoints(routes: Any, _depth: int = 0) -> Iterator[Any]:
    """递归展开路由树，兼容 APIRoute 列表与 `_IncludedRouter`（惰性 include）。"""
    if _depth > 10:  # 防御：畸形路由树不要无限递归
        return
    for route in routes or ():
        if _is_endpoint(route):
            yield route
            continue
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from iter_endpoints(contexts(), _depth + 1)
            continue
        sub_routes = getattr(route, "routes", None)
        if sub_routes:
            yield from iter_endpoints(sub_routes, _depth + 1)


def _auth_and_scopes(route: Any) -> tuple[bool, tuple[str, ...]]:
    authed = False
    scopes: list[str] = []

    # 端点函数自身也可能是依赖（`Depends(require_scope(...))` 只会出现在 dependant 树里，
    # 但 `security=` 或函数签名里的 Principal 会让 get_current_owner 出现在叶子节点）。
    for dep in _iter_dependant(route.dependant):
        call = dep.call
        if call is None:
            continue
        if getattr(call, "__rbac_auth__", False) or getattr(call, "__name__", "") == "get_current_owner":
            authed = True
        scopes.extend(getattr(call, "__rbac_scopes__", ()))
    return authed, tuple(dict.fromkeys(scopes))


def scan_app(app: FastAPI) -> Report:
    """扫描全部路由。返回的报告只描述问题，不做任何修改。"""
    report = Report()
    seen_public: set[tuple[str, str]] = set()

    for route in iter_endpoints(app.routes):
        methods = sorted({m.upper() for m in route.methods} - SKIP_METHODS)
        authed, scopes = _auth_and_scopes(route)

        for scope in scopes:
            if scope not in ALL_SCOPES:
                report.violations.append(
                    Violation(
                        KIND_UNKNOWN_SCOPE,
                        "/".join(methods),
                        route.path,
                        f"scope {scope!r} 不在 rbac.ALL_SCOPES 里（拼错或未登记）",
                    )
                )

        for method in methods:
            report.total += 1
            key = (method, route.path)
            is_public = key in PUBLIC_ENDPOINTS

            if is_public:
                seen_public.add(key)
                report.public.append(f"{method} {route.path}")
                continue

            if route.path.endswith(_PROBE_SUFFIXES) and method == "GET":
                # `/_status` 是骨架自检端点，公开且只回模块名，不含任何业务数据。
                report.public.append(f"{method} {route.path}")
                continue

            if not authed:
                report.violations.append(
                    Violation(
                        KIND_MISSING_AUTH,
                        method,
                        route.path,
                        "未挂认证依赖（OwnerDep / require_scope）",
                    )
                )
                continue

            if method in WRITE_METHODS and not scopes:
                report.violations.append(
                    Violation(
                        KIND_MISSING_SCOPE,
                        method,
                        route.path,
                        "写端点未挂 require_scope()——只靠前端隐藏按钮等于没有权限控制",
                    )
                )

    for method, path in sorted(PUBLIC_ENDPOINTS - seen_public):
        report.violations.append(
            Violation(KIND_STALE_PUBLIC, method, path, "PUBLIC_ENDPOINTS 里的豁免已无对应路由，删掉它")
        )

    return report


def check_role_table() -> list[str]:
    """角色表自检：三档角色必须存在，owner 必须是通配符。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    from .rbac import ROLE_SCOPES, WILDCARD  # 局部导入避免循环引用时的初始化顺序问题

    for role in ROLES:
        if role not in ROLE_SCOPES:
            problems.append(f"角色 {role} 未在 ROLE_SCOPES 中定义")
    if WILDCARD not in ROLE_SCOPES.get("owner", frozenset()):
        problems.append("owner 必须持有通配符 *，否则新增 scope 会把自己锁在门外")
    return problems
