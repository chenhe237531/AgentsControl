# execution · 执行面与 Worker/Orca

- **归属窗口**：W06
- **路由前缀**：`/execution`
- **职责**：Worker 协议 claim/heartbeat/complete/fail；Orca adapter 六原子动作；worktree 即任务载体。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /workers/claim`
- `POST /workers/{id}/complete`
- `GET /sessions`
- `POST /sessions/{id}/steer`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
