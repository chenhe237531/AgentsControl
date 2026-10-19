# ledger · 双币种账本

- **归属窗口**：W07
- **路由前缀**：`/ledger`
- **职责**：钱六分项 + 注意力（min/outcome）、CPO 口径、订阅摊销、成本归集到任务/目标。

## 端点清单（W07 已实现）

| 端点 | 说明 |
|---|---|
| `POST /entries` | 成本归集（幂等：同 `source+source_id` 回放返回 200，不重复记账） |
| `GET /entries` | 明细查询（task/goal/category/period/limit 过滤） |
| `GET /summary` | 周期六分项 + 注意力指标（缺失返回 null / 0） |
| `GET /attention` | min/outcome：期内注意力分钟 ÷ 达标产出 |
| `GET /budget-status` | 预算余量与是否冻结（W02-I06 / W03-I04 只读契约） |
| `POST /subscriptions` | 登记订阅（摊销 + 配额消耗率） |
| `GET /subscriptions` | 订阅列表（当期摊销、消耗率、>80% 告警位） |
| `PATCH /subscriptions/{id}` | 更新配额用量 / 启停 |
| `GET /reconcile` | 对账：账本 vs 来源，差异 > 1% 标红（`dry_run` 参数） |
| `GET /export.csv` | 当期明细 CSV（带 BOM） |

骨架自检端点 `/_status`、`/_ping` 暂按原样保留：
`/_status` 的响应被 `tests/test_smoke.py::test_module_status_is_stub` 钉死（W01 的文件，
本窗口不可改），待 W01 放宽后一并删除（见 `.fleet/handoff/W07.md`）。

## 纯函数（无 IO，可穷举单测）

- `attention.py`：`month_bounds` / `compute_min_per_outcome`
- `amortize.py`：`amortize_calendar`（自然月均摊、月中新增折算）/ `amortize_usage` / `quota_consumption` / `quota_alert`
- `reconcile.py`：`reconcile_sources`（阈值比较与标红）

## 边界

本目录之外的文件不要改。需要改共享文件时，走 `x-shared-registry` 变更请求（见 `docs/DEV-PLAN-v1.md` §7）。
