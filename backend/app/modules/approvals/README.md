# approvals · 闸门与带宽熔断

- **归属窗口**：W07
- **路由前缀**：`/approvals`
- **职责**：L0/L1/L2/L3 决策分级、审批流、带宽熔断（日决策数/否决率异常/深夜禁 L0）。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `GET /queue`
- `POST /{id}/decide`
- `GET /bandwidth`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
