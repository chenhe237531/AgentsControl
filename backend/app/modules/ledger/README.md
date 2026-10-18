# ledger · 双币种账本

- **归属窗口**：W07
- **路由前缀**：`/ledger`
- **职责**：钱六分项 + 注意力（min/outcome）、CPO 口径、订阅摊销、成本归集到任务/目标。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `GET /summary`
- `GET /entries`
- `POST /entries`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
