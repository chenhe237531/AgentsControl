# github_sync · GitHub 验收总线

- **归属窗口**：W09
- **路由前缀**：`/github`
- **职责**：Issue 镜像、PR 事件、checks 结果、verdict（ok/rework）、熔断（CI 连败 3 次转人工）。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `POST /webhook`
- `GET /pulls`
- `GET /issues`
- `POST /sync`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
