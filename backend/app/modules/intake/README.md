# intake · 收件箱（体感入口）

- **归属窗口**：W02
- **路由前缀**：`/intake`
- **职责**：一句话捕获、AI 三问结构化、14 天自动归档为兴趣池、转目标。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /items`
- `GET /items`
- `POST /items/{id}/structure`
- `POST /items/{id}/promote`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
