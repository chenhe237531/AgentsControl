# tasks · 对外契约（W03）

其他窗口只依赖本文件。实现细节（SQL、状态机内部）不作为契约。

## 1. 只读字段（跨窗口）

| 表.字段 | 谁读 | 语义 / 注意 |
|---|---|---|
| `tasks.goal_id` | W02 | 可为空；**无目标的任务不进账本统计** |
| `tasks.requires` (JSONB) | W04 / W06 | `{gui, orca, browser, always_on}`，四个键都存在，缺省 `false` |
| `tasks.priority` | W06 / W10 | 0–9，**数字越小越优先** |
| `tasks.state` | 全部 | 见下；**任何模块不得直接 UPDATE** |
| `tasks.cost_spent` | W07 | 由 W06/W07 记账写入，W03 的 HTTP 接口只读 |
| `dead_letter` | W07 | 死信 100% 计入账本「返工」分项 |

## 2. 状态与事件

```
queued  --lease-->  leased  --start-->  running  --complete-->  done
   |                  |                   |
   |                  +--reclaim---------+--> queued
   |                  +--reclaim_exhausted--> dead
   |                  +--fail--> failed --retry/requeue--> queued
   |                              +--give_up--> dead
   +--hold--> held --release--> queued
   +--cancel--> cancelled（除 done 外均可取消）
```

- `failed` 的 `retry` 由后台脚本按 `next_retry_at` 自动触发，**最多 `max_retries` 次**，指数退避（30 s / 60 s / 120 s，封顶 1 h）。
- `dead` 只能由人调用 `POST /tasks/{id}/requeue` 复活，**永不自动**。
- `held` 是预算阀门的产物：超预算任务不排队，停在这里并带 `hold_reason`。

## 3. 端点（全部需要 `Authorization: Bearer <access token>`）

| 方法 | 路径 | 关键字段 |
|---|---|---|
| POST | `/tasks` | `title` 必填；`priority` 0–9；`requires`；`budget_cap`；`max_retries`；`retryable` |
| GET | `/tasks` | `state`（可重复）、`device_id`、`goal_id`、`priority_min/max`、`overdue_only`、`limit`、`offset` |
| GET | `/tasks/{id}` | 返回 `queued_seconds` / `queued_overdue` 派生字段 |
| GET | `/tasks/{id}/candidates` | `candidates[]{device_id,name,eligible,reasons[]}` + `note` |
| POST | `/tasks/claim` | 入参 `device_id`、`caps`、`lease_seconds?`；返回 `LeaseOut` 或 **204** |
| POST | `/tasks/{id}/lease` | 入参 `device_id`、`caps`、`lease_seconds?` |
| POST | `/tasks/{id}/heartbeat` | 入参 `lease_token` |
| POST | `/tasks/{id}/start` | 入参 `lease_token` |
| POST | `/tasks/{id}/complete` | 入参 `lease_token`、`result?` |
| POST | `/tasks/{id}/fail` | 入参 `lease_token`、`error`、`retryable?` |
| POST | `/tasks/{id}/cancel` | 入参 `reason` |
| POST | `/tasks/{id}/requeue` | 入参 `reason`、`reset_counters`（默认 true） |
| GET | `/tasks/dead` | `include_resolved`、`limit`、`offset` |
| GET | `/tasks/budget-status` | 汇总值，不扫明细 |

`caps` 的键：`has_gui` / `has_orca` / `has_browser` / `can_sleep`。
**不传 = 不声明能力**：只会领到无能力要求的任务（这是故意的，避免静默错派）。

## 4. 错误码（前端按 `code` 映射文案，不解析 message）

| code | HTTP | 含义 | 工作端应做什么 |
|---|---|---|---|
| `illegal_transition` | 409 | 当前状态不接受该事件 | 刷新任务状态，停止重试 |
| `lease_conflict` | 409 | 同一任务被别的 worker 抢先 | 不要重试同一 task，去 claim 下一个 |
| `lease_lost` | 409 | 令牌失效（被回收/转手） | **立刻停止执行**，丢弃本地结果 |
| `capability_mismatch` | 409 | 设备能力不满足 | 不要重试；上报能力或换设备 |
| `budget_exceeded` | 409 | 预算不足，任务已转 `held` | 不要重试；等人工加预算或改任务 |

`error.details` 中：`illegal_transition` 带 `allowed_events`；`capability_mismatch` 带 `reasons[]`（可直接展示）。

## 5. 给其他窗口的扩展点

- **W06**：领取后按 `heartbeat_seconds`（默认 30）续租；收到 `lease_lost` 必须 kill 会话。
  需要「不重跑已完成的活」时用 `lease_token` 做幂等键。
- **W07**：记账完成后调用 `app.modules.tasks.service.record_cost(session, task_id, amount)`，
  不要直接写 `tasks.cost_spent`；预算总览直接读 `GET /tasks/budget-status`。
- **W10**：`GET /tasks` 的 `queued_overdue` 可直接渲染为告警角标；
  `GET /tasks/dead` 的 `detail` 是给人看的中文原因。
