# 开发计划与 10 窗口并行拆分 · v1

**日期**：2026-10-18
**上游文档**：`TECH-ARCHITECTURE-v1.md`（技术与阶段）、`PRODUCT-DESIGN-v0.2.md`（产品约束与 P0–P4）、`INTEGRATION-ORCA.md`（执行面边界）
**机器可读清单**：`plan/windows.yaml`（窗口/issue/波次/依赖）、`plan/README.md`（运行手册）
**状态总表**：`plan/STATUS.md`（由脚本从 git + gh 重建）
**合并机制**：`docs/MERGE-REVIEW-v1.md`

---

## 0. 这份文档要解决什么

10 个 Agent 同时在一个仓库里写代码，失败原因几乎从不是"能力不够"，而是这四件事：

| 失败模式 | 症状 | 本方案的对应机制 |
|---|---|---|
| **改同一个文件** | 每个 PR 都冲突，合并变成手工劳动 | 目录级所有权 + 冻结文件清单（§5） |
| **不知道下一步做什么** | 开窗口第一件事是"我该干嘛" | 每窗口一份 `plan/Wxx.md`，issue 内部顺序已定（§4） |
| **上下文丢失** | 新对话要重新解释一切 | 状态外置到文件：handoff + 勾选表（§8） |
| **合并即失守** | 边界与迁移在两周内失效 | 边界脚本化断言 + 迁移单头 + OpenAPI 快照 + 合并波次（§9） |

一句话原则：

> **并行靠"目录不重叠"，不靠"沟通及时"。** 任何需要两个窗口商量才能决定的事，都应该提前写进边界表。

---

## 1. 阶段先后顺序

### 1.1 第一段：基本框架（已完成，第一个 commit）

这一步必须在拆分前做完，因为它是所有窗口的**共同依赖**。已交付：

| 交付物 | 路径 | 作用 |
|---|---|---|
| FastAPI 骨架 | `backend/app/main.py` | 中间件顺序、生命周期、异常处理注册 |
| 集中配置 + 启动校验 | `backend/app/core/config.py` | 生产环境强校验，带病启动不可能 |
| 类型化错误体系 | `backend/app/core/errors.py` | `error.code` 稳定，前端据此做文案 |
| 结构化 JSON 日志 | `backend/app/core/logging.py` | request_id 贯穿 + 敏感字段脱敏 |
| DB / Redis / 安全原语 | `core/db.py`、`core/redis.py`、`core/security.py` | 连接池、带 TTL 缓存、JWT 与 HMAC |
| **集中路由注册（冻结）** | `backend/app/api/router.py` | 10 个模块一次性注册，永不改 |
| 10 个模块占位 | `backend/app/modules/*` | 每个带 README 声明 owner 与端点清单 |
| 迁移框架 | `backend/migrations/`（`0001_baseline`） | Alembic + 只增不改的纪律 |
| 前端骨架 | `web/`（Next.js 15 + Tailwind + TanStack Query） | 10 个路由占位、**导航注册表冻结**、类型化 API 客户端 |
| Go Agent 骨架 | `agent/` | 三通道循环、HMAC 签名、`Buffer` 接口、协议类型 |
| 部署 | `deploy/docker-compose.yml`、`deploy/Caddyfile` | 单机 4C8G 全栈 + 自动 HTTPS |
| CI | `.github/workflows/ci.yml` | 六路并行 job + 唯一必过检查 `gate` |
| 计划与边界 | `plan/`、`scripts/check_boundaries.py` | 本方案的可执行部分 |

**验收**：见 §10。全部通过才允许开窗口。

### 1.2 第二段：10 窗口并行（本方案主体）

按产品阶段重排为 **4 个合并波次**（波次是合并顺序，不是开发顺序）：

```
Wave 1（第 1 周）  地基模块先落地
  W01-I01 认证          W03-I01 任务状态机       W07-I01 账本模型
  W04-I01 设备注册      W09-I05 预检脚本         W10-I01 设计令牌 / I02 今日页
  W02-I01 收件箱捕获    W05-I01 心跳采集         W06-I01 Worker 协议     W08-I01 评测框架

Wave 2（第 2 周）  核心链路打通，前后端第一次真正连起来
  W01-I02 RBAC          W02-I02 三问 / I03 目标卡    W03-I02 租约 / I03 能力匹配
  W04-I02 验签 / I03 心跳机 / I04 事件            W05-I02 SQLite 缓冲
  W06-I02 probe / I03 worktree                    W07-I02 注意力 / I03 闸门
  W08-I02 雷达 / I03 能力上限                     W09-I01 webhook / I02 PR / I03 Issue
  W10-I03 收件箱&目标页

Wave 3（第 3 周）  加固与联动，开始出现跨窗口聚合接口
  W01-I03 吊销 / I04 限流    W02-I04 模板 / I05 兴趣池    W03-I04 预算阀门 / I05 公平调度
  W04-I05 快照    W05-I03 服务安装 / I04 唤醒    W06-I04 会话控制 / I05 harvest
  W07-I04 熔断 / I05 订阅摊销    W08-I04 漂移 / I05 skill    W09-I04 CI 熔断
  W10-I04 执行&团队 / I05 账本&兵工厂

Wave 4（第 4 周）  收口：审计、可视化、发布链
  W01-I05 内核补齐    W02-I06 目标聚合    W03-I06 死信    W04-I06 指令白名单
  W05-I05 打包 / I06 断网    W06-I06 会话视图 API    W07-I06 对账导出
  W08-I06 认知卡    W09-I06 CI 门禁    W10-I06 拓扑视图 / I07 E2E
```

**为什么这样切波次**：窗口可以一直并行写，但**合并**必须按波次推进。
否则 W02 的页面会在 W07 的接口还没定型时就合进来，后面被迫返工。
越往后波次越"聚合/收口"，正是为了让前一波的接口先稳定。

### 1.3 第三段：阶段性毕业（对应产品 P0–P4）

| 阶段 | 由谁交付 | 毕业标准（达不到就停，不硬推） |
|---|---|---|
| P0 带宽闭环 | W02 + W07 + W10 | 连续 7 天每天用它看完板并立项，日均 ≤10 分钟，产出 ≥3 个可验收目标 |
| P1 单兵闭环 | W06 + W03 + W07 + W08 | 1 个熵减目标跑完，省下的钱 > 系统当月成本 |
| P2 小队闭环 | W03 + W08 + W10 | 3 个目标并行，日决策 ≤12，返工率 ≤20% |
| P3 规模 | W04 + W05 + W06 | 150 个 Agent，决策负载不上升（唯一硬指标） |
| P4 外化 | 不在 10 窗口内 | 3 个外部用户能独立跑通 P1 |

> **注意**：P0 的毕业标准量的是"你用它"，不是"代码写完"。所以 W10-I02（今日页）排在 Wave 1，
> 而不是等所有后端做完——**先让人有东西可用，再补机械**。

---

## 2. 10 个窗口总览

| 窗口 | 名称 | 负责模块（目录） | issue | 主责阶段 |
|---|---|---|---|---|
| **W01** | 身份、权限与工程内核 | `core/`、`modules/identity/`、`docs/adr/` | 5 | T0–T2 |
| **W02** | 收件箱与双轨目标 | `modules/intake/`、`modules/goals/` | 6 | P0 |
| **W03** | 任务队列与租约 | `modules/tasks/` | 6 | P1–P3 |
| **W04** | 设备接入 | `modules/devices/` | 6 | T1–T2 |
| **W05** | Go 设备 Agent | `agent/`、`deploy/agent/` | 6 | T1–T2 |
| **W06** | 执行面 / Orca | `modules/execution/` | 6 | P1–T3 |
| **W07** | 账本 / 闸门 / 熔断 | `modules/ledger/`、`modules/approvals/` | 6 | P0–P1 |
| **W08** | 资产面 / 模型雷达 | `modules/evals/` | 6 | P1–P2 |
| **W09** | GitHub 验收总线 | `modules/github_sync/`、`scripts/`、`.github/workflows/` | 6 | T3 |
| **W10** | 前端控制台与可视化 | `web/` | 7 | P0–T4 |

合计 **60 个 issue**，每窗口 5–7 个。

**为什么是 10 个**：窗口数受"模块边界自然分割数"限制。按 `router.py` 的 10 个模块切，边界最清晰；
再多就要拆同一个模块（冲突率陡增），再少则一个窗口干 2 个模块（被迫串行，白丢并行度）。
**60 个 issue 的规模是刻意选的**：一个 Agent 窗口一天能做 1–1.5 个 issue，一周刚好做完一个窗口。

---

## 3. 目录所有权表

> 完整版（含每个 issue 的 `files` 与 `depends_on`）：`plan/windows.yaml`。校验：`python scripts/check_boundaries.py`。

| 窗口 | 独占（可写） | 只读依赖 | 禁止 |
|---|---|---|---|
| W01 | `backend/app/core/**`、`modules/identity/**`、`tests/test_core*.py`、`tests/test_identity.py`、`backend/scripts/**`、`docs/adr/**` | — | `api/router.py`、`web/`、`agent/` |
| W02 | `modules/intake/**`、`modules/goals/**`、`tests/test_intake.py`、`tests/test_goals.py` | `tasks.service`（读） | `core/**`、`web/` |
| W03 | `modules/tasks/**`、`tests/test_tasks.py` | `devices.caps`、`ledger.summary`（读） | `core/**` |
| W04 | `modules/devices/**`、`tests/test_devices.py` | `core/security.verify_hmac` | `agent/**` |
| W05 | `agent/**`、`deploy/agent/**` | `agent/internal/protocol/types.go`（共管） | `backend/**` |
| W06 | `modules/execution/**`、`tests/test_execution.py` | `tasks.service`、Orca CLI | `modules/tasks/**`（禁写） |
| W07 | `modules/ledger/**`、`modules/approvals/**`、`tests/test_ledger.py`、`tests/test_approvals.py` | tasks/goals 聚合（读） | `core/**` |
| W08 | `modules/evals/**`、`tests/test_evals.py` | LiteLLM（外部 HTTP） | `web/**` |
| W09 | `modules/github_sync/**`、`tests/test_github.py`、`tests/fixtures/github/`、`scripts/pr_precheck.sh`、`scripts/openapi_snapshot.py`、`scripts/check_boundaries.py`、`.github/workflows/**` | GitHub API | 任何 `modules/*/` 业务代码 |
| W10 | `web/**` | 全部 `GET` 接口 + `POST /auth/*` | `backend/app/**`、`agent/**` |

**跨窗口读依赖只有两种合法方式**：① 调对方 `service.py` 的公开函数；② 只读 SQL。
需要对方加新接口时，写进自己的 issue（如"W03-I04 需要 `GET /ledger/budget-status`"），由对方排期。

---

## 4. issue 内部执行顺序的三条规则

每个窗口的 `plan/Wxx.md` 已把顺序定死。这里说明**为什么**，方便遇到情况时自己判断：

1. **契约先于实现**：涉及端点的 issue，先写 schema + 迁移 + 空实现（501），再填逻辑。
   这样别的窗口能立刻对着 schema 写前端，不用等。
2. **纯函数先于 IO**：状态机、匹配器、熔断判定都写成不查库的纯函数。它们是 bug 高发区，纯函数能穷举单测。
3. **幂等先于并发**：先证明"重复调用结果一致"，再谈"并发不崩"。顺序反了会得到只在压测时出现的幽灵 bug。

---

## 5. 冻结文件与变更请求

### 5.1 清单

| 文件 | 为什么冻结 | 谁能改 |
|---|---|---|
| `backend/app/api/router.py` | 10 个窗口都想加一行 include_router，是 100% 冲突点 | 仅在波次边界由 W01 合入 |
| `web/src/config/nav.ts` | 同上（每个窗口都想加菜单） | 同上 |
| `backend/app/core/**` | 9 个窗口的公共依赖，改一次全体重测 | W01，走 shared-registry |
| `agent/internal/protocol/types.go` | 跨端契约，单边改动造成静默数据错乱 | **W04 + W05 在同一 PR 里改** |
| `plan/windows.yaml` | 计划即契约 | 仅 Marshal，波次边界 |
| `.github/workflows/ci.yml` | 门禁定义，不能被单窗口放松 | W09 |

### 5.2 shared-registry 变更请求

```
1. 开 issue，label: x-shared-registry
2. 标题：[shared] 需要给 config 增加 TASK_LEASE_SECONDS
3. 正文写：改什么 / 为什么必须改 / 影响哪些窗口
4. 单独分支、单独 PR，PR 里不含业务代码
5. W01 审 + Marshal 在波次边界合并
6. 合并后各窗口 rebase
```

代价是慢半天，收益是不用做 10 次三方合并。

---

## 6. worktree 开发流

### 6.1 一次性准备

```bash
cd /Users/nitai/Documents/WorkCode/Agent/AgentsControl
./scripts/new-window.sh W01     # 建分支 + worktree + handoff 初稿，并打印开场 prompt
```

命名：

```
分支      win/w<NN>-<slug>          例：win/w03-task-queue
worktree  .worktrees/w<NN>-<slug>   例：.worktrees/w03-task-queue
```

**为什么窗口一个长期分支而不是 issue 一分支**：issue 之间串行依赖，每 issue 一分支会造 60 个分支和 60 次 rebase。
窗口分支 + 每 issue 一组提交，进度一样可追踪（脚本按提交消息的 issue 号解析），但只维护 10 个分支。

### 6.2 每个 issue 的固定六步

```
1. plan/Wxx.md 把该 issue 改为 DOING
2. git pull --rebase origin main      ← 只在 issue 开始时做一次
3. 写代码 + 测试 + ./scripts/pr_precheck.sh --local
4. 刷新 .fleet/handoff/Wxx.md（≤10 行）
5. commit（含 (#issue)）+ push
6. 更新 PR 描述 + plan/Wxx.md 改为 REVIEW / DONE
```

### 6.3 PR 策略

每窗口**一个长期 Draft PR**（标题 `[W03] 任务队列与租约`），描述里维护 issue 勾选表。

好处：CI 每 push 都跑；进度可见；不用管 60 个 PR；Vercel Preview 只有一个链接。
合并时按波次把该波涉及的提交 squash 后合并，然后继续在同一分支做下一波。

---

## 7. 边界检查（把文档变成断言）

文档会过期，脚本不会。`scripts/check_boundaries.py`：

| 检查 | 失败含义 | 阻断 |
|---|---|---|
| 改动是否全在窗口 `allow` 内 | 越界 | 是 |
| 改冻结文件但无 `x-shared-registry` | 绕过变更流程 | 是 |
| `allow` 路径是否存在 / 窗口路径是否互相嵌套 | **规则本身写错了** | 是 |
| 迁移头是否唯一、`downgrade()` 是否存在 | 迁移分叉/不可回滚 | 是 |
| 是否存在冲突标记 | 半解决冲突被提交 | 是 |
| 是否提交 `.env`/密钥 | 泄露 | 是 |
| OpenAPI 是否有变化 | 契约变更 | 否（警告 + 要求在 PR 说明） |

---

## 8. 进度追踪与中断续跑

### 8.1 三层状态

| 层 | 载体 | 更新时机 | 谁看 |
|---|---|---|---|
| **权威状态** | GitHub（分支/commit/PR/checks） | 每次 push 自动 | 机器 |
| **人读状态** | `plan/STATUS.md` | `./scripts/window-status.sh --write` 重建 | 你 |
| **接手状态** | `.fleet/handoff/Wxx.md` + `plan/Wxx.md` 勾选表 | 每个 issue 结束 | 下一个 Agent |

**原则：状态必须在文件里，不能在对话里。** 上下文易失，文件持久。

### 8.2 进度的单一事实来源：commit 消息

```
feat(#42): 实现刷新令牌轮换
```

`scripts/window-status.sh` 靠 `git log --grep` + `gh pr checks` 重建进度，输出示例：

```
W03 任务队列与租约   分支 win/w03-task-queue   PR #14 (Draft, checks: ✓)
  W03-I01 任务模型与状态机   DONE  a1b2c3d
  W03-I02 租约与心跳回收     TODO
  进度 1/6  █▌········ 16%
```

### 8.3 三种中断，三种恢复

| 情形 | 恢复动作 | 成本 |
|---|---|---|
| 对话被关掉，代码已提交 | 读 `plan/Wxx.md` → 第一个非 DONE 的 issue → 按 `handoff` 的"未完成"继续 | < 5 分钟 |
| 改了没提交（崩溃） | `git status` → 提交为 WIP → 继续 | < 10 分钟 |
| 做半个 issue 发现方向错 | `handoff` 写清"为什么错" → 回退到该 issue 起点 commit | 半个 issue |

10 个窗口的开场 prompt 见 `plan/PROMPTS.md`（可直接复制给对应 Agent）。

---

## 9. 合并检查与评审（摘要，全文见 `docs/MERGE-REVIEW-v1.md`）

### 9.1 四道闸

```
① 自动门禁 CI gate   编译/测试/迁移单头/边界/密钥  —— 全机器判定，红就断
② 边界审查（脚本）   越界？冻结文件？改的是不是自己那摊
③ 契约审查（人 10min）OpenAPI diff、迁移可回滚、错误码合规
④ 正确性审查（人 15min）验收标准逐条对照、测试是否真断言、遗留是否写清
```

### 9.2 顺序纪律

- 波次内按依赖顺序合并；同模块 issue 必须串行。
- 每天最多 3 个 PR；周五下午不合并。
- 合并前必须 rebase 最新 main 并**重跑** CI，不允许用旧绿灯。
- 只有 **Merge Marshal** 有合并权限；窗口只负责把 PR 弄绿并申请。

### 9.3 main 变红的唯一姿势

```
git revert <merge commit> && push    → 让 main 先绿
修复在分支上做，重新申请合并         → 不在 main 上 hotfix
```

---

## 10. 骨架交付自检（已实测）

| # | 检查 | 命令 | 结果 |
|---|---|---|---|
| 1 | 后端单测 | `cd backend && python -m pytest` | **17 passed**（含 10 个占位 skip → 实际 17 通过 10 skip） |
| 2 | 服务启动 | `uvicorn app.main:app --port 8000` | 成功，结构化 JSON 日志 |
| 3 | liveness | `curl /health` | `{"status":"ok",...}` |
| 4 | readiness（依赖缺失时降级） | `curl /ready` | 503 + `database.ok=false`（预期：本地未起 PG） |
| 5 | 10 模块注册 | `curl /openapi.json` | 23 paths，含 10 个 `_status` |
| 6 | 认证链路 | 无 token → 401 / 有 token → 501 | 均正确 |
| 7 | Go Agent 编译 | `cd agent && GOCACHE=/tmp/gocache go build ./...` | 通过；`afc-agent -version` 正常 |
| 8 | 迁移单头 | `python scripts/check_boundaries.py` | 0 error 0 warning |
| 9 | 边界脚本 | 同上 | 10 窗口 / 60 issue |
| 10 | OpenAPI 快照 | `python scripts/openapi_snapshot.py --check` | 无变化 |
| 11 | 预检脚本 | `bash scripts/pr_precheck.sh --local` | 见输出 |
| 12 | 生产配置校验 | `APP_ENV=prod pytest tests/test_core.py` | 缺 secret 时报错 |

> **前端说明**：本机沙箱对 `node_modules` 的批量写入与符号链接有拦截（`CODEBUDDY_BROKER_DENY`），
> `npm install` 可能失败。这不影响开工：W10 的 I01/I02 只改 `src/` 下的源码，
> 构建验证在正常终端执行 `npm install && npm run build` 一次即可。

---

## 11. 风险与应对（针对"并行"这个玩法）

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 两个窗口改同一文件 | 中 | 中 | 目录所有权 + 冻结清单 + CI 边界检查 |
| 迁移分叉 | **高** | 高 | 命名规范 `w<NN>_<seq>_<slug>.py` + 单头断言 + roundtrip |
| 某窗口卡住阻塞下游 | 中 | 高 | 每 issue 有降级实现（501 或 mock），下游按契约先写 |
| Agent 顺手"优化"别的模块 | **高** | 中 | 边界脚本阻断 + PR 模板第一条自查 |
| 60 个 issue 做不完 | 中 | 高 | 每窗口先做 Wave 1 两个 issue；4 周做不完就砍 Wave 4（全是收口，不影响主链路） |
| 文档与代码脱节 | 高 | 中 | 边界/迁移/OpenAPI 全部脚本化；STATUS 每次重建 |
| 上下文污染（读了别人计划） | 中 | 中 | 开场 prompt 明确"只读三个文件" |

---

## 12. 现在就开始的命令

```bash
cd /Users/nitai/Documents/WorkCode/Agent/AgentsControl

./scripts/window-status.sh                  # 全局进度
./scripts/new-window.sh W03                 # 创建 W03 的 worktree 并打印开场 prompt
python scripts/check_boundaries.py          # 计划与边界自检
bash scripts/pr_precheck.sh --local         # 提交前预检
```
