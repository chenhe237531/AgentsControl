# tasks · 任务队列与租约

- **归属窗口**：W03
- **路由前缀**：`/tasks`
- **职责**：Postgres 队列（`FOR UPDATE SKIP LOCKED`）、租约/心跳/回收、能力标签匹配、预算阀门、死信与重试。

## 分层

| 文件 | 职责 | 不许做 |
|---|---|---|
| `router.py` | HTTP 入参/出参 | 业务判断、直接写库 |
| `service.py` | 编排与规则，事务边界 | import Request/Response；写 `UPDATE` |
| `repository.py` | **唯一的写路径**（队列 SQL 都在这） | 业务判断 |
| `machine.py` | 状态机（唯一迁移真相源） | IO |
| `matcher.py` | 能力匹配纯函数 + 对应的 SQL 谓词 | 查库 |
| `valve.py` | 预算阀门纯函数 + 对应的 SQL 谓词 | 算账本明细 |
| `scheduler.py` | 优先级分档、防饿死、退避 | 查库 |
| `models.py` | ORM | 业务逻辑 |

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 创建任务；超预算直接进 `held` |
| GET | `/tasks` | 列表（按 `priority` 升序、`queued_at` 升序）；支持 `state` / `device_id` / `goal_id` / `priority_min,max` / `overdue_only` |
| GET | `/tasks/{id}` | 详情（含派生的 `queued_overdue`） |
| GET | `/tasks/{id}/candidates` | 候选设备 + 不可派原因（只读 W04 的 `devices`） |
| POST | `/tasks/claim` | 领取下一个任务（能力 + 预算 + 公平性过滤，单条 SQL）；无活返回 204 |
| POST | `/tasks/{id}/lease` | 指定任务领租（同样走能力/预算校验） |
| POST | `/tasks/{id}/heartbeat` | 续租（默认 30 s 一次，租约 90 s） |
| POST | `/tasks/{id}/start` | `leased → running` |
| POST | `/tasks/{id}/complete` | 交货 → `done` |
| POST | `/tasks/{id}/fail` | 失败上报：可重投则指数退避，否则进死信 |
| POST | `/tasks/{id}/cancel` | 取消 |
| POST | `/tasks/{id}/requeue` | **人工**复活死信/失败任务（唯一复活入口） |
| GET | `/tasks/dead` | 死信列表（含原因、次数、是否已处理） |
| GET | `/tasks/budget-status` | 预算总览（只读预聚合值） |

`lease_token` 只在 `claim` / `lease` 的响应里出现一次；之后的心跳、交货、失败上报都必须带上它。
令牌不匹配返回 `409 lease_lost`——工作端应立刻放弃当前任务，而不是继续跑。

## 后台脚本

| 脚本 | 用途 |
|---|---|
| `scripts/reclaim_leases.py` | 回收过期租约（`--dry-run` 只打印 id）+ 把到达退避时间的 `failed` 放回队列；幂等 |
| `scripts/bench_claim.py` | 领取延迟压测，输出 P50/P95，用于 PR 描述 |

## 配置（环境变量）

`backend/app/core/**` 是冻结文件，本窗口未直接改 `core/config.py`，
参数从同名环境变量读取（`TASK_LEASE_SECONDS` 等，见 `settings.py`），
已提交 shared-registry 变更请求，合并后 `settings.py` 退化为适配层。

硬约束：`TASK_LEASE_SECONDS >= 2 × TASK_HEARTBEAT_SECONDS`，不满足则启动即失败。

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
`cost_spent` 只由 W06/W07 通过 `service.record_cost()` 写，本模块的 HTTP 接口只读。
