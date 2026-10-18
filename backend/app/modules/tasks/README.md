# tasks · 任务队列与租约

- **归属窗口**：W03
- **路由前缀**：`/tasks`
- **职责**：Postgres 队列（FOR UPDATE SKIP LOCKED）、租约/心跳/回收、能力标签匹配、预算阀门。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /`
- `GET /`
- `POST /{id}/lease`
- `POST /{id}/heartbeat`
- `POST /{id}/complete`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
