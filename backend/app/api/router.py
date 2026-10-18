"""集中路由注册点 —— **冻结文件（FROZEN）**。

为什么冻结：10 个 Agent 窗口并行开发，如果每人都在这里加一行 include_router，
这个文件会成为 100% 的冲突热点。因此骨架阶段一次性把全部模块路由注册好，
窗口只在自己的 `app/modules/<module>/router.py` 内新增端点，永不修改本文件。

新增模块的唯一合法路径：
  1) 在 `plan/boundaries.yaml` 登记归属窗口；
  2) 提交 shared-registry 变更请求（label: x-shared-registry），由 W01 合并窗口统一落地；
  3) 合并波次只允许在 Wave 边界（见 docs/DEV-PLAN-v1.md §6）执行。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules import (
    approvals,
    devices,
    evals,
    execution,
    github_sync,
    goals,
    identity,
    intake,
    ledger,
    tasks,
)

api_router = APIRouter()

# 顺序即 OpenAPI 中的分组顺序，保持稳定以免快照抖动。
for _module in (identity, intake, goals, tasks, devices, execution, ledger, approvals, evals, github_sync):
    api_router.include_router(_module.router)
