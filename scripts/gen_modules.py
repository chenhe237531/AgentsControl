"""生成 10 个业务模块的占位包（骨架一次性生成，窗口只在自己目录内迭代）。"""

from pathlib import Path

MODULES = [
    # (包名, 路由前缀, 窗口, 中文名, 职责, 关键端点占位)
    (
        "identity",
        "/auth",
        "W01",
        "身份与权限",
        "登录/刷新/登出、RBAC 判定、会话与令牌吊销；单用户白名单，不开放注册。",
        ["POST /login", "POST /refresh", "POST /logout", "GET /me"],
    ),
    (
        "intake",
        "/intake",
        "W02",
        "收件箱（体感入口）",
        "一句话捕获、AI 三问结构化、14 天自动归档为兴趣池、转目标。",
        ["POST /items", "GET /items", "POST /items/{id}/structure", "POST /items/{id}/promote"],
    ),
    (
        "goals",
        "/goals",
        "W02",
        "双轨目标与模板库",
        "赚钱/进化双轨验收、预算与 TTL、止损判定、8 个模板的默认三问答案。",
        ["GET /", "POST /", "GET /{id}", "PATCH /{id}", "GET /templates"],
    ),
    (
        "tasks",
        "/tasks",
        "W03",
        "任务队列与租约",
        "Postgres 队列（FOR UPDATE SKIP LOCKED）、租约/心跳/回收、能力标签匹配、预算阀门。",
        ["POST /", "GET /", "POST /{id}/lease", "POST /{id}/heartbeat", "POST /{id}/complete"],
    ),
    (
        "devices",
        "/devices",
        "W04",
        "设备接入",
        "注册与 token 哈希、心跳/增量/快照三通道、(node_id,seq) 幂等、suspect/offline 状态机、能力标签。",
        ["POST /register", "POST /heartbeat", "POST /events", "POST /snapshot", "GET /"],
    ),
    (
        "execution",
        "/execution",
        "W06",
        "执行面与 Worker/Orca",
        "Worker 协议 claim/heartbeat/complete/fail；Orca adapter 六原子动作；worktree 即任务载体。",
        ["POST /workers/claim", "POST /workers/{id}/complete", "GET /sessions", "POST /sessions/{id}/steer"],
    ),
    (
        "ledger",
        "/ledger",
        "W07",
        "双币种账本",
        "钱六分项 + 注意力（min/outcome）、CPO 口径、订阅摊销、成本归集到任务/目标。",
        ["GET /summary", "GET /entries", "POST /entries"],
    ),
    (
        "approvals",
        "/approvals",
        "W07",
        "闸门与带宽熔断",
        "L0/L1/L2/L3 决策分级、审批流、带宽熔断（日决策数/否决率异常/深夜禁 L0）。",
        ["GET /queue", "POST /{id}/decide", "GET /bandwidth"],
    ),
    (
        "evals",
        "/evals",
        "W08",
        "资产面与模型雷达",
        "20 道自有题 × N 模型、8 维得分、能力上限标记、漂移预警、skill/harness 版本与认知卡。",
        ["GET /radar", "POST /runs", "GET /skills", "GET /cards"],
    ),
    (
        "github_sync",
        "/github",
        "W09",
        "GitHub 验收总线",
        "Issue 镜像、PR 事件、checks 结果、verdict（ok/rework）、熔断（CI 连败 3 次转人工）。",
        ["POST /webhook", "GET /pulls", "GET /issues", "POST /sync"],
    ),
]

TEMPLATE = '''"""{cn}模块 —— owner: {window}

{desc}

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

MODULE = "{name}"
OWNER_WINDOW = "{window}"

router = APIRouter(prefix="{prefix}", tags=["{name}"])


@router.get("/_status", summary="模块占位状态（骨架自检用）")
async def module_status() -> dict[str, str]:
    return {{"module": MODULE, "owner_window": OWNER_WINDOW, "state": "stub"}}


@router.get("/_ping", summary="需要认证的占位端点（验证认证链路）")
async def module_ping(_: OwnerDep) -> dict[str, str]:
    raise NotImplementedYetError(f"模块 {{MODULE}} 尚未实现，owner={{OWNER_WINDOW}}")
'''

README = """# {name} · {cn}

- **归属窗口**：{window}
- **路由前缀**：`{prefix}`
- **职责**：{desc}

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

{endpoints}

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
"""

root = Path(__file__).resolve().parents[1]
mod_dir = root / "backend" / "app" / "modules"
mod_dir.mkdir(parents=True, exist_ok=True) if not mod_dir.exists() else None

for name, prefix, window, cn, desc, endpoints in MODULES:
    d = mod_dir / name
    if not d.exists():
        d.mkdir()
    (d / "__init__.py").write_text(f'from .router import router  # noqa: F401\n\n__all__ = ["router"]\n', encoding="utf-8")
    (d / "router.py").write_text(
        TEMPLATE.format(cn=cn, window=window, desc=desc, name=name, prefix=prefix), encoding="utf-8"
    )
    (d / "README.md").write_text(
        README.format(
            name=name,
            cn=cn,
            window=window,
            prefix=prefix,
            desc=desc,
            endpoints="\n".join(f"- `{e}`" for e in endpoints),
        ),
        encoding="utf-8",
    )

(mod_dir / "__init__.py").write_text(
    '"""业务模块包。每个模块对应一个归属窗口，模块之间只通过 service 接口或事件表通信。"""\n',
    encoding="utf-8",
)
print(f"generated {len(MODULES)} modules")
