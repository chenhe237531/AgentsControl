# goals · 双轨目标与模板库

- **归属窗口**：W02
- **路由前缀**：`/goals`
- **职责**：赚钱/进化双轨验收、预算与 TTL、止损判定、8 个模板的默认三问答案。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `GET /`
- `POST /`
- `GET /{id}`
- `PATCH /{id}`
- `GET /templates`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
