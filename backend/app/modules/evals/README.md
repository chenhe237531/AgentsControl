# evals · 资产面与模型雷达

- **归属窗口**：W08
- **路由前缀**：`/evals`
- **职责**：20 道自有题 × N 模型、8 维得分、能力上限标记、漂移预警、skill/harness 版本与认知卡。

## 端点清单（骨架占位，窗口按 PLAN 顺序实现）

- `GET /radar`
- `POST /runs`
- `GET /skills`
- `GET /cards`

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
