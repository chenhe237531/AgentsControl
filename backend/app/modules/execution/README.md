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
| POST | `/api/v1/execution/sessions` | 创建会话：建 worktree + `.fleet-task.json`（幂等防重派） | W06-I03 |
| DELETE | `/api/v1/execution/sessions/{id}` | 回收会话：脏 worktree 拒绝并返回文件清单（`?force=1` 强制丢弃） | W06-I03 |
| POST | `/api/v1/execution/sessions/{id}/spawn` | 启动会话（probe 可用才派；转 running 并自动续租） | W06-I04 |
| POST | `/api/v1/execution/sessions/{id}/send` | 向 running 会话注入 prompt；会话已死 → 任务 failed + 租约释放 | W06-I04 |
| POST | `/api/v1/execution/sessions/{id}/steer` | 人工闸门/纠偏（生命周期级指令） | W06-I04 |
| POST | `/api/v1/execution/sessions/{id}/kill` | 终止会话并释放租约（kill 即释放；graceful 先 snapshot） | W06-I04 |

## 认证

并行开发期沿用 OwnerDep（owner 令牌）；Worker 独立身份待 W01-I02 RBAC 落地后收紧。

## 对 W03 的契约期望（handoff）

`tasks.service` 需提供 `lease / renew_lease / complete / fail / release`（可选）
公开函数，签名见 `protocol.py` 模块文档；未交付时本模块统一 502 `tasks_unavailable`。
