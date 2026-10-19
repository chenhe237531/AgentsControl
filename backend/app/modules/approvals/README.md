# approvals · 闸门与带宽熔断

- **归属窗口**：W07
- **路由前缀**：`/approvals`
- **职责**：L0/L1/L2/L3 决策分级、审批流、带宽熔断（日决策数/否决率异常/深夜禁 L0）。

## 端点清单（W07 已实现）

| 端点 | 说明 |
|---|---|
| `POST /` | 创建审批（L0 ≤2/日、L1 ≤8/日，超限自动转明日：`visible_at` 顺延） |
| `GET /queue` | 待决策队列（默认 L0/L1；抽检页传 `levels=L2,L3`） |
| `POST /{id}/decide` | 决策（重复 409；拒绝即记录；夜间 L0 → 202 只记"待明日确认"） |
| `GET /bandwidth` | 带宽熔断快照（输入快照 + 纯函数判定，阈值来自 `AFC_BREAKER_*` 环境变量） |

骨架自检端点 `/_status`、`/_ping` 暂按原样保留（原因见 ledger/README.md 与 `.fleet/handoff/W07.md`）。

## 纯函数（无 IO，可穷举单测）

- `breaker.py`：`evaluate`（四条规则）/ `is_night` / `load_breaker_config`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
