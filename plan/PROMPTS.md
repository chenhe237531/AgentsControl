# 10 窗口开场 Prompt（直接复制）

> 用法：把对应窗口的整段 prompt 粘贴给一个新的 Agent 对话。
> 每段都刻意只让它读三个文件——**读得越少，越不容易误改别人的东西**。

---

## 通用前置（只在第一次给，之后每个窗口都已在 prompt 里说明）

已就绪的事实，不需要再解释：

- 仓库：`/Users/nitai/Documents/WorkCode/Agent/AgentsControl`（私有，main 已有骨架 commit）
- 技术栈：FastAPI + Postgres + Alembic / Next.js 15 / Go Agent / Docker Compose + Caddy
- 骨架已完成：配置、错误、日志、认证原语、10 个模块占位、迁移框架、CI `gate`、边界脚本
- 冻结文件：`backend/app/api/router.py`、`web/src/config/nav.ts`、`backend/app/core/**`、
  `agent/internal/protocol/types.go`、`plan/windows.yaml`、`.github/workflows/ci.yml`
- 任何越界改动都会被 CI `plan` job 阻断

---

## W01 · 身份、权限与工程内核

```
你是 W01 窗口（身份、权限与工程内核）。工作目录 /Users/nitai/Documents/WorkCode/Agent/AgentsControl/.worktrees/w01-identity-kernel
（若不存在先跑 ./scripts/new-window.sh W01）

只读这三个文件，不要读其它窗口的计划：
  1. plan/W01.md
  2. plan/README.md
  3. .fleet/handoff/W01.md

规则：
- 从第一个状态不是 DONE 的 issue 开始，严格串行，一次只做一个 issue。
- 只改 allow 列表内的路径（backend/app/core/、modules/identity/、backend/tests/test_core*.py、test_identity.py、backend/scripts/、docs/adr/）。
- 每完成一个 issue：改 plan/W01.md 状态 → 刷新 handoff → commit "feat(#N): ..." → push。
- 提交前必须 ./scripts/pr_precheck.sh --local 通过，否则不许提交。
- 需要改冻结文件时停下，开 x-shared-registry issue，不要顺手改。
```

---

## W02 · 收件箱与双轨目标

```
你是 W02 窗口（收件箱与双轨目标）。工作目录 /Users/nitai/Documents/WorkCode/Agent/AgentsControl/.worktrees/w02-intake-goals

只读三个文件：plan/W02.md、plan/README.md、.fleet/handoff/W02.md

这个窗口有三条产品硬规则，不许"优化"掉（详见 plan/W02.md §2）：
- Inbox 记录成本 ≤30 秒：只要求一个字符串，任何必填项增加都会杀死这个入口
- AI 三问固定：验收 / TTL / 预算；第一问答不出来必须建议"放兴趣池"，不许硬造目标
- 14 天未结构化自动归档为"兴趣"，不催办

只改 backend/app/modules/intake/、modules/goals/、tests/test_intake.py、test_goals.py。
严格串行 I01→I06，每 issue 一次 commit+push，提交前跑 ./scripts/pr_precheck.sh --local。
LLM 调用只走 LITELLM_BASE_URL，不在模块里直连厂商。
```

---

## W03 · 任务队列与租约

```
你是 W03 窗口（任务队列与租约，控制总线）。工作目录 .worktrees/w03-task-queue

只读三个文件：plan/W03.md、plan/README.md、.fleet/handoff/W03.md

最容易翻车的四点（plan/W03.md §2 有展开）：
- 队列必须是 Postgres FOR UPDATE SKIP LOCKED，不是 Redis
- 租约 90s > 心跳 30s，否则正常任务被误回收（表现为"任务反复重启"）
- 回收 3 次进死信，绝不静默丢弃
- 能力标签不匹配就不派；always_on 任务绝不派给可休眠设备

只改 backend/app/modules/tasks/ 与 backend/tests/test_tasks.py。
状态迁移只允许写在 machine.py 的字典里，任何地方不许直接 UPDATE state。
严格串行 I01→I06，每 issue 一次 commit+push + 预检通过。
```

---

## W04 · 设备接入

```
你是 W04 窗口（设备接入，数据总线）。工作目录 .worktrees/w04-device-ingest

只读三个文件：plan/W04.md、plan/README.md、.fleet/handoff/W04.md

三条不可退让的语义（plan/W04.md §2）：
- 心跳 60s / 增量 30s 批量带游标 / 快照 60min，三者不能合并
- 幂等靠 UNIQUE(node_id, seq)，不是应用层去重；重复投递返回 200 不是 409
- token 只存哈希；第三��设备不接收任何指令；丢弃必须上报 dropped_count

只改 backend/app/modules/devices/ 与 backend/tests/test_devices.py。
你不碰 agent/：跨端契约是 agent/internal/protocol/types.go，要改必须和 W05 一起改。
严格串行 I01→I06。
```

---

## W05 · Go 设备 Agent

```
你是 W05 窗口（Go 设备 Agent）。工作目录 .worktrees/w05-go-agent

只读三个文件：plan/W05.md、plan/README.md、.fleet/handoff/W05.md

关键约束：
- 单二进制、无 CGO（SQLite 用 modernc.org/sqlite），否则跨平台交叉编译会坏
- Buffer 接口不变，只替换实现（sqlite 版要保证 seq 跨重启单调不减）
- 采集失败不能影响进程存活，未知指标上 -1（不要用 0，0 会被当成"空闲"）
- 测试必须 go test ./... -race，gofmt -l . 必须为空

只改 agent/ 与 deploy/agent/。不碰 backend/。
首次 go build 很慢，可用 GOCACHE=/tmp/gocache go build ./... 预热。
严格串行 I01→I06。
```

---

## W06 · 执行面（Worker 协议与 Orca）

```
你是 W06 窗口（执行面）。工作目录 .worktrees/w06-execution

只读三个文件：plan/W06.md、plan/README.md、.fleet/handoff/W06.md

边界（最容易越界的一处）：
- 你负责"把任务变成运行中的会话"，不负责"派给谁"（W03）、"预算够不够"（W07）、"PR 合不合规"（W09）
- 不直接改 tasks 表，只调 tasks.service 的公开函数
- 不在请求处理器里跑长任务：spawn 立刻返回，过程写事件表
- 只依赖 Orca 的四个稳定事实（CLI/worktree/PTY/hooks），不碰它的 UI 与数据库

只改 backend/app/modules/execution/ 与 backend/tests/test_execution.py。
严格串行 I01→I06。
```

---

## W07 · 账本、闸门与带宽熔断

```
你是 W07 窗口（治理面）。工作目录 .worktrees/w07-ledger-approvals

只读三个文件：plan/W07.md、plan/README.md、.fleet/handoff/W07.md

最容易做错的三件事：
- 只记钱不记注意力 → 整个"省你的脑子"的设计会落空，min/outcome 必须与 ¥ 同屏
- CPO 分母用任务数 → 应除以"T+7 达标产出"，否则多做无用功看起来更划算
- 闸门做成"什么都问人" → L0 ≤2、L1 ≤8 每日，超了自动转明日，不给"全部通过"按钮

金额用 NUMERIC(14,4) 不用 float；每条 entry 必须可追溯到 task_id 或 goal_id。
判定逻辑写成纯函数（breaker.py），不查库，便于穷举单测。
只改 modules/ledger/、modules/approvals/、tests/test_ledger.py、test_approvals.py。
```

---

## W08 · 资产面（模型雷达 / skill / 认知卡）

```
你是 W08 窗口（资产面）。工作目录 .worktrees/w08-assets-radar

只读三个文件：plan/W08.md、plan/README.md、.fleet/handoff/W08.md

这个模块要回答的不是"哪个模型最强"，而是"哪类任务现在根本做不到，所以别立项"。
因此：题面用自己的 20 个真实任务（不用公开榜单）；必须输出能力上限标记（全模型 <70% 标红）；
必须输出漂移预警（周环比 >3pct）。

成本控制是硬要求：单次评测成本上限可配、结果缓存 7 天、cost 必须真实写入账本（不许成账外支出）。
只改 backend/app/modules/evals/ 与 backend/tests/test_evals.py。
```

---

## W09 · GitHub 验收总线与 CI 工具链

```
你是 W09 窗口（GitHub 验收总线）。工作目录 .worktrees/w09-github-oracle

只读三个文件：plan/W09.md、plan/README.md、.fleet/handoff/W09.md

核心判断：GitHub 是 oracle，不要让模型自评。
- merged + checks 绿 → verdict=ok；closed 未合并 → rework；CI 连败 3 次 → 熔断转人工
- 不做双向同步：只从 GitHub 拉，绝不往 GitHub 推状态
- webhook 先落库再处理，5 秒内返回 200，重活进后台

你独占地拥有 scripts/ 与 .github/workflows/，且不能放松任何检查。
先把 I05（预检脚本）做完——其它 9 个窗口都在等它。
只改 modules/github_sync/、tests/test_github.py、tests/fixtures/github/、scripts/$你的文件、.github/。
```

---

## W10 · 前端控制台与可视化

```
你是 W10 窗口（前端控制台）。工作目录 .worktrees/w10-console-ui

只读三个文件：plan/W10.md、plan/README.md、.fleet/handoff/W10.md

三条产品约束比组件库重要：
- 今日页只有 3 个数字 + 8 条决策卡 + 一句"今天到此为止"，不加图表不加列表不加"更多"
- 默认只看异常，正常任务折叠成一行统计
- 项目视图要能发现三件事：撞车、阻塞、偏离（不是"看着爽"）

API 纪律：只用 web/src/lib/api/client.ts，禁止组件里 fetch；令牌不放 localStorage；
4xx 不重试、5xx 自动重试（providers.tsx 已统一）；错误必须映射成中文文案 + request_id。
只改 web/，不碰 backend/app/。
I01→I07 串行；后端接口没就绪时用符合 OpenAPI 的 mock，并在注释标 TODO(sync)。
```

---

## 中断后恢复（任意窗口通用）

```
继续 W<NN> 窗口。工作目录 .worktrees/w<NN>-<slug>

按顺序做三件事，然后直接开工，不要向我确认计划：
  1. 读 plan/W<NN>.md，找第一个状态不是 DONE 的 issue
  2. 读 .fleet/handoff/W<NN>.md，看上次停在哪（与 plan 冲突时以 handoff 为准）
  3. git log --oneline -5 确认最后一次提交到哪
然后从第 1 步找到的 issue 继续，严格串行，做完立刻 commit+push 并更新状态表。
```
