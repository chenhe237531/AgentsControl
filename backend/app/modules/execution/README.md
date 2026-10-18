# execution 模块

- **归属窗口**：W06（Worker 协议与 Orca 适配）
- **交付分支**：`win/w06-execution`
- **边界**：不写 `tasks` 表（任务状态机归 W03），只通过 `tasks.service` 公开函数流转；
  不做任务拆解、预算扣减（W04）、PR 归集（W07）。

## 数据表（自有）

| 表 | 语义 |
|---|---|
| `execution_workers` | Worker 注册表（claim 登记，heartbeat touch） |
| `task_events` | 任务事件流（claimed/completed/failed/cost/…；W07 只读归集，W10 只读展示） |
| `execution_sessions` | 会话 = 租约 + worktree（W06-I03 起） |

## 端点清单

| 方法 | 路径 | 语义 | issue |
|---|---|---|---|
| GET | `/api/v1/execution/_status` | 模块自检 | 骨架 |
| POST | `/api/v1/execution/workers/claim` | Worker 领活，无任务 204 | W06-I01 |
| POST | `/api/v1/execution/workers/{id}/heartbeat` | 续约（带进度才落事件） | W06-I01 |
| POST | `/api/v1/execution/workers/{id}/complete` | 交货（result 必带产物引用+成本） | W06-I01 |
| POST | `/api/v1/execution/workers/{id}/fail` | 报错（retryable 语义） | W06-I01 |
| GET | `/api/v1/execution/orca/health` | Orca 能力自检（缓存 10 分钟，`?force=1` 强制重探） | W06-I02 |

## 认证

并行开发期沿用 OwnerDep（owner 令牌）；Worker 独立身份待 W01-I02 RBAC 落地后收紧。

## 对 W03 的契约期望（handoff）

`tasks.service` 需提供 `lease / renew_lease / complete / fail / release`（可选）
公开函数，签名见 `protocol.py` 模块文档；未交付时本模块统一 502 `tasks_unavailable`。
