# 开发计划与 10 窗口并行拆分 · v1

**日期**：2026-10-18
**上游文档**：`TECH-ARCHITECTURE-v1.md`（技术与阶段）、`PRODUCT-DESIGN-v0.2.md`（产品约束与 P0–P4）、`INTEGRATION-ORCA.md`（执行面边界）、`INPUT-CHANNELS-v1.md`（信息通道）
**机器可读清单**：`plan/windows.yaml`（窗口、issue、波次、依赖）、`plan/boundaries.yaml`（文件边界）
**状态总表**：`plan/STATUS.md`（由脚本从 git + gh 生成，可随时重建）

---

## 0. 这份文档要解决什么

10 个 Agent 同时在一个仓库里写代码，失败原因几乎从不是"能力不够"，而是这四件事：

| 失败模式 | 症状 | 本方案的对应机制 |
|---|---|---|
| **改同一个文件** | 每个 PR 都冲突，合并变成手工劳动 | 目录级所有权 + 冻结文件清单（§5） |
| **不知道下一步做什么** | 开窗口第一件事是"我该干嘛" | 每个窗口一份 `plan/Wxx.md`，issue 内部顺序已定（§4） |
| **上下文丢失** | 新对话要重新解释一切 | 状态外置到文件：`handoff` + 勾选表，不靠对话历史（§8） |
| **合并即失守** | 界线和迁移在两周内失效 | 边界脚本化断言 + 迁移单头 + OpenAPI 快照 + 合并波次（§9） |

一句话原则：

> **并行靠"目录不重叠"，不靠"沟通及时"。** 任何需要两个窗口商量才能决定的事，都应该提前写进边界表。

---

## 1. 阶段先后顺序（先做什么，后做什么）

### 1.1 第一段：基本框架（已完成，对应 commit `chore: 项目基础框架`）

这一步必须在拆分前做完，因为它是所有窗口的**共同依赖**。已交付：

| 交付物 | 路径 | 作用 |
|---|---|---|
| FastAPI 骨架 | `backend/app/main.py` | 中间件顺序、生命周期、异常处理注册 |
| 集中配置 + 启动校验 | `backend/app/core/config.py` | 生产环境强校验，带病启动不可能 |
| 类型化错误体系 | `backend/app/core/errors.py` | `error.code` 稳定，前端据此做文案 |
| 结构化 JSON 日志 | `backend/app/core/logging.py` | request_id 贯穿 + 敏感字段脱敏 |
| DB / Redis / 安全原语 | `core/db.py`、`core/redis.py`、`core/security.py` | 连接池、TTL 缓存、JWT 与 HMAC |
| **集中路由注册（冻结）** | `backend/app/api/router.py` | 10 个模块一次性注册，永不改 |
| 10 个模块占位 | `backend/app/modules/*` | 每模块一带 `README` 声明 owner 与端点清单 |
| 迁移框架 | `backend/migrations/`（`0001_baseline`） | Alembic + 只增不改的纪律 |
| 前端骨架 | `web/`（Next.js 15 + Tailwind + TanStack Query） | 10 个路由占位、**导航注册表冻结**、类型化 API 客户端 |
| Go Agent 骨架 | `agent/` | 三通道循环、HMAC 签名、`Buffer` 接口、协议类型 |
| 部署 | `deploy/docker-compose.yml`、`deploy/Caddyfile` | 单机 4C8G 全栈 + 自动 HTTPS |
| CI | `.github/workflows/ci.yml` | 五路并行 job + 必跑的 `gate` |
| 计划与边界 | `plan/` | 本方案的可执行部分 |

**验收**：见 §10「骨架交付自检」。全部通过才允许开窗口。

### 1.2 第二段：10 窗口并行（本方案主体）

按 `PRODUCT-DESIGN-v0.2` 的阶段重排为 **4 个合并波次**（不是 4 个开发阶段，窗口一直并行）：

```
Wave 1（第 1 周）  地基模块先落地，其余窗口可开工但先不合并
  W01-I01 认证        W03-I01 任务状态机     W07-I01 账本模型
  W04-I01 设备注册    W09-I05 预检脚本       W10-I01 设计令牌 / I02 今日页
  W02-I01 收件箱捕获  W05-I01 心跳采集       W06-I01 Worker 协议    W08-I01 评测框架

Wave 2（第 2 周）  核心链路打通，前后端第一次真正连起来
  W01-I02 RBAC        W02-I02 三问 / I03 目标卡    W03-I02 租约 / I03 能力匹配
  W04-I02 验签 / I03 心跳机 / I04 事件   W05-I02 SQLite 缓冲
  W06-I02 probe / I03 worktree     W07-I02 注意力 / I03 闸门
  W08-I02 雷达 / I03 能力上限      W09-I01 webhook / I02 PR / I03 Issue
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

**为什么这样切波次**：Wave 是**合并顺序**，不是开发顺序。窗口可以一直并行写，
但**合并**必须按波次推进——否则 W02 的页面会在 W07 的接口还没定型时就合并进来，
后面被迫返工。波次越往后，跨窗口依赖越多，所以后面几波刻意做成"聚合/收口"性质。

### 1.3 第三段：阶段性毕业（对应产品 P0–P4）

| 阶段 | 本方案中由谁交付 | 毕业标准（达不到就停，不硬推） |
|---|---|---|
| P0 带宽闭环 | W02（收件箱/目标）+ W07（闸门）+ W10（今日页） | 连续 7 天每天用它看完板并立项，日均 ≤10 分钟，产出 ≥3 个可验收目标 |
| P1 单兵闭环 | W06（1 Worker）+ W03（租约）+ W07（账本）+ W08（雷达） | 1 个熵减目标跑完，省下的钱 > 系统当月成本 |
| P2 小队闭环 | W03（公平调度）+ W08（skill/harness）+ W10（项目视图） | 3 个目标并行，日决策 ≤12，返工率 ≤20% |
| P3 规模 | W04（多设备）+ W05（三平台打包）+ W06（Orca L2） | 150 个 Agent，决策负载不上升（唯一硬指标） |
| P4 外化 | 不在 10 窗口内（课程与客户交付） | 3 个外部用户能独立跑通 P1 |

> **注意**：P0 的毕业标准量的是"你用它"，不是"代码写完"。所以 W10-I02（今日页）排在 Wave 1，
> 而不是等所有后端做完。**先让人有东西可用，再补机械。**

---

## 2. 10 个窗口总览

| 窗口 | 名称 | 负责模块（目录） | issue 数 | Wave 分布 | 主责阶段 |
|---|---|---|---|---|---|
| **W01** | 身份、权限与工程内核 | `core/`、`modules/identity/`、`docs/adr/` | 5 | 1,2,3,4 | T0–T2 |
| **W02** | 收件箱与双轨目标 | `modules/intake/`、`modules/goals/` | 6 | 1,2,3,4 | P0 |
| **W03** | 任务队列与租约 | `modules/tasks/` | 6 | 1,2,3,4 | P1–P3 |
| **W04** | 设备接入 | `modules/devices/` | 6 | 1,2,3,4 | T1–T2 |
| **W05** | Go 设备 Agent | `agent/`、`deploy/agent/` | 6 | 1,2,3,4 | T1–T2 |
| **W06** | 执行面 / Orca | `modules/execution/` | 6 | 1,2,3,4 | P1–T3 |
| **W07** | 账本 / 闸门 / 熔断 | `modules/ledger/`、`modules/approvals/` | 6 | 1,2,3,4 | P0–P1 |
| **W08** | 资产面 / 模型雷达 | `modules/evals/` | 6 | 1,2,3,4 | P1–P2 |
| **W09** | GitHub 验收总线 | `modules/github_sync/`、`scripts/`、`.github/workflows/` | 6 | 1,2,3,4 | T3 |
| **W10** | 前端控制台与可视化 | `web/` | 7 | 1,2,3,4 | P0–T4 |

合计 **60 个 issue**。每窗口 5–7 个，正好是一个 Agent 窗口在一周内能做完并留下可合并增量的量。

**为什么是 10 个而不是 6 个或 15 个**：窗口数受"模块边界自然分割数"限制。
按 `router.py` 里的 10 个模块切，边界最清晰；再多就得拆同一个模块（冲突率陡增），
再少则一个窗口要做 2 个模块（顺序被迫串行，并行度白丢）。

---

## 3. 窗口 → 模块 → 文件 的所有权表

> 完整版见 `plan/boundaries.yaml`（机器可读，`scripts/check_boundaries.py` 会校验）。

| 窗口 | 独占（可写） | 只读依赖 | 冻结（禁改） |
|---|---|---|---|
| W01 | `backend/app/core/**`、`backend/app/modules/identity/**`、`backend/tests/test_core*.py`、`backend/tests/test_identity*.py`、`backend/scripts/**`、`docs/adr/**` | — | `api/router.py` |
| W02 | `backend/app/modules/intake/**`、`modules/goals/**`、`tests/test_intake*.py`、`tests/test_goals*.py` | `tasks.service`（不写） | `core/**` |
| W03 | `backend/app/modules/tasks/**`、`tests/test_tasks*.py` | `devices.caps`（读）、`ledger.summary`（读） | `core/**` |
| W04 | `backend/app/modules/devices/**`、`tests/test_devices*.py` | `core/security.verify_hmac` | `agent/**`（契约文件除外） |
| W05 | `agent/**`、`deploy/agent/**` | `agent/internal/protocol/types.go`（双方共同拥有） | `backend/**` |
| W06 | `backend/app/modules/execution/**`、`tests/test_execution*.py` | `tasks.service`、`orca CLI` | `modules/tasks/**`（禁写） |
| W07 | `backend/app/modules/ledger/**`、`modules/approvals/**`、`tests/test_ledger*.py`、`tests/test_approvals*.py` | `tasks`/`goals` 聚合（读） | `core/**` |
| W08 | `backend/app/modules/evals/**`、`tests/test_evals*.py` | LiteLLM（外部 HTTP） | `web/**` |
| W09 | `backend/app/modules/github_sync/**`、`tests/test_github*.py`、`scripts/pr_precheck.sh`、`scripts/openapi_snapshot.py`、`.github/workflows/**` | GitHub API | 任何 `modules/*/` 业务代码 |
| W10 | `web/**` | 全部 `GET` 接口 + `POST /auth/*` | `backend/app/**`、`agent/**` |

**跨窗口读依赖的规则**：只调用**对方的 service 公开函数**或**只读 SQL**，绝不直接改对方的表。
需要对方提供新接口时，在 issue 里写清楚（如 W03-I04 需要 `GET /ledger/budget-status`），由对方在自己的计划里排。

---

## 4. issue 内部执行顺序的三条规则

每个窗口的 `plan/Wxx.md` 已经把顺序定死，这里说明**为什么这么定**，方便遇到情况时自己判断：

1. **契约先于实现**：涉及端点的 issue，先写 Pydantic schema + 迁移 + 空实现（返回 501），
   再填业务逻辑。这样其它窗口可以立刻对着 schema 写前端，不用等。
2. **纯函数先于 IO**：状态机（`machine.py`）、匹配器（`matcher.py`）、熔断判定（`breaker.py`）
   都写成不查库的纯函数。它们是 bug 高发区，纯函数能单测穷举，省掉 80% 的调试时间。
3. **幂等先于并发**：先证明"重复调用结果一致"，再谈"并发不崩"。
   顺序反了会得到一堆只在压测时出现的幽灵 bug。

---

## 5. 冻结文件与变更请求（防止冲突的核心机制）

### 5.1 冻结文件清单

| 文件 | 为什么冻结 | 谁能改 |
|---|---|---|
| `backend/app/api/router.py` | 10 个窗口都会想加一行 `include_router`，是 100% 冲突点 | 只在 Wave 边界由 W01 合入 |
| `web/src/config/nav.ts` | 同上（每个窗口都想加菜单项） | 同上 |
| `backend/app/core/*.py` | 9 个窗口都依赖，改一次全体重测 | 走 shared-registry 变更请求 |
| `agent/internal/protocol/types.go` | 跨端契约，单边改动会造成静默数据错乱 | W04 + W05 双方在同一 PR 改 |
| `plan/windows.yaml`、`plan/boundaries.yaml` | 计划本身，改了就等于改契约 | 只在 Wave 边界，且需记录变更原因 |
| `.github/workflows/ci.yml` | 门禁定义，不能让任何单窗口放松自己的检查 | W09 独占，且改动需说明 |

### 5.2 shared-registry 变更请求流程

需要改冻结文件时，**不要在功能 PR 里顺手改**，而是：

```
1. 开 issue，label: x-shared-registry
2. 标题格式：[shared] 需要给 config 增加 TASK_LEASE_SECONDS
3. 正文写三件事：改什么、为什么必须改、影响哪些窗口
4. 只在该 issue 的分支上改冻结文件，单独提 PR（PR 里不含业务代码）
5. 由 W01 审 + 合并，选在 Wave 边界的合并窗口
6. 合并后，各窗口 rebase 自己的分支
```

**代价是慢半天，收益是不用做 10 次三方合并。**

---

## 6. worktree 开发流（每个窗口怎么开工）

### 6.1 一次性准备

```bash
cd /Users/nitai/Documents/WorkCode/Agent/AgentsControl
git fetch origin

# 每个窗口一个 worktree + 一个长期分支（不是每个 issue 一个分支）
git worktree add .worktrees/w01-identity-kernel -b win/w01-identity-kernel origin/main
git worktree add .worktrees/w02-intake-goals    -b win/w02-intake-goals    origin/main
# ... W03..W10 同理（脚本见 scripts/new-window.sh）
```

**为什么窗口一个长期分支，而不是 issue 一个分支**：
issue 之间是串行依赖（I02 依赖 I01），每个 issue 单独开分支会制造 60 个分支和 60 次 rebase。
窗口分支配合"每 issue 一个 commit（或一组 commit）"，进度一样可追踪，且只有 10 个分支要维护。

分支命名：`win/w<NN>-<slug>`；worktree 目录：`.worktrees/w<NN>-<slug>`（已在 `.gitignore`）。

### 6.2 每个 issue 的固定节奏（6 步，不可省）

```
1. 在 plan/Wxx.md 把该 issue 状态改为 DOING
2. git pull --rebase origin main       ← 只在 issue 开始时做，不在中途做
3. 写代码 + 写测试 + 本地跑 scripts/pr_precheck.sh
4. 刷新 .fleet/handoff/Wxx.md（≤10 行）
5. commit（含 (#issue) 号）+ push
6. 更新 PR 描述 + 把 plan/Wxx.md 状态改为 REVIEW / DONE
```

**粗提交粒度**：一个 issue 允许 3–8 个 commit（WIP 也可以），但**每个 commit 都要 push**。
理由是断电/崩溃最多丢半小时，而不是丢一天。

### 6.3 PR 策略

- **每个窗口只开一个 Draft PR**，标题 `[W01] 身份与内核`，描述里维护 issue 勾选表。
- 好处：① CI 每 push 都跑；② 进度对自己可见；③ 不用管理 60 个 PR；
  ④ Vercel PR Preview 只有一个链接，方便随时点开看。
- 合并时**不是整个 PR 一次性合并**，而是按波次把该波涉及的 commit **squash 成 1–2 个提交**后合并
  （见 §9.3 的合并方式），然后继续在同一分支上做下一波。

> 反直觉但重要：**长期分支 + 波次合并** 比 **每 issue 一分支** 的总合并成本低得多，
> 前提是分支上每个 commit 都能独立编译通过。CI 会对每个 push 验证这一点。

---

## 7. 边界检查脚本（把边界变成断言）

文档会过期，脚本不会。`scripts/check_boundaries.py` 在 CI 里跑，检查：

| 检查 | 失败含义 | 阻断 |
|---|---|---|
| PR 改动的文件是否全在该窗口 `allow` 列表内 | 越界改动 | 是 |
| 是否改了冻结文件且 PR 没有 `x-shared-registry` 标签 | 绕过变更流程 | 是 |
| `alembic heads` 是否恰好 1 个 | 迁移分叉 | 是 |
| 是否有冲突标记 `<<<<<<<` | 半解决冲突被提交 | 是 |
| 是否提交了 `.env` / 密钥文件 | 密钥泄露 | 是 |
| OpenAPI 快照是否变化 | 契约变更（需评审确认） | 否（警告 + 要求 PR 里说明） |
| 新增迁移是否可 downgrade | 无法回滚 | 是 |

这些检查全部是"读 git diff + 跑一条命令"，秒级完成，不拖慢 CI。

---

## 8. 进度追踪与中断续跑

### 8.1 三层状态（各有各的用途）

| 层 | 载体 | 更新时机 | 谁看 |
|---|---|---|---|
| **权威状态** | GitHub（分支、commit、PR、checks） | 每次 push 自动 | 机器、你复盘时 |
| **人读状态** | `plan/STATUS.md` | 随时用脚本重建 | 你每天扫一眼 |
| **接手状态** | `.fleet/handoff/Wxx.md` + `plan/Wxx.md` 勾选表 | 每个 issue 结束时 | 下一个 Agent 窗口 |

**核心原则**：状态必须在**文件**里，不能在对话里。上下文是易失的，文件是持久的。

### 8.2 进度的单一事实来源：commit 消息

```
feat(#42): 实现刷新令牌轮换     ← issue 号必须在括号里
```

`scripts/window-status.sh` 就是靠 `git log --grep` + `gh pr checks` 重建进度的：

```bash
./scripts/window-status.sh            # 全部窗口
./scripts/window-status.sh W03        # 单个窗口
./scripts/window-status.sh --watch    # 每 30 秒刷新（盯多窗口时用）
```

输出示例：

```
W03 任务队列与租约            分支 win/w03-task-queue    PR #14 (Draft, checks: ✓)
  I01 任务模型与状态机     DONE    3 commits   a1b2c3d
  I02 租约与心跳回收       DOING   1 commit    e4f5g6h
  I03 能力匹配             TODO
  ...
  进度 2/6  最近提交 42 分钟前
```

### 8.3 中断后如何继续（三种中断，三种恢复方式）

| 中断情形 | 恢复动作 | 恢复成本 |
|---|---|---|
| **窗口对话被关掉**，代码已提交 | 新对话读 `plan/Wxx.md`，找到第一个非 `DONE` 的 issue，从 `handoff` 的"未完成"接着做 | < 5 分钟 |
| **代码改了但没提交**（崩溃/断电） | `git status` 看残留 → `git stash` 或直接提交为 WIP → 再继续 | < 10 分钟 |
| **做了半个 issue 发现方向错** | 在 `handoff` 里写清"为什么错"，回退到 issue 起点的 commit，重新做 | 半个 issue |

**新窗口的开场 prompt（固定模板，直接复制）**：

```
你是 W03 窗口。仓库 /Users/nitai/Documents/WorkCode/Agent/AgentsControl。
只读这三个文件，不要读其它窗口的计划：
  1. plan/W03.md            （你的 issue 清单与顺序）
  2. plan/README.md         （全局约定）
  3. .fleet/handoff/W03.md  （上次交接）
工作目录：.worktrees/w03-task-queue
从第一个状态不是 DONE 的 issue 开始，严格串行，做完一个立刻 commit+push 并更新状态。
如果 handoff 与 plan 冲突，以 handoff 为准（它更新）。
```

### 8.4 开工前的"骨架防漂移"检查

新会话开始前跑一次，防止别人的合并把本地搞坏：

```bash
./scripts/pr_precheck.sh --local
```

---

## 9. 合并检查与评审机制

详见 `docs/MERGE-REVIEW-v1.md`。这里是摘要：

### 9.1 四道闸

```
① 自动门禁（CI gate）    编译 + 测试 + 迁移单头 + 边界 + 冲突标记 + 密钥扫描
② 边界审查（脚本）       check_boundaries.py：越界/冻结文件/调度权限
③ 契约审查（人）         OpenAPI diff、迁移可回滚、错误码是否符合表
④ 正确性审查（人/Agent） 验收标准逐条对照 + 关键路径测试是否真的断言了行为
```

### 9.2 合并顺序纪律

- **同一波次内，按"依赖在前"合并**：`W01-I01 → W03-I01 → W06-I01`。
- **同一模块的 issue 必须串行合并**（W02 的 I02 不能在 I01 前面）。
- **每天最多合并 3 个 PR**，且不在周五下午合并（出问题没人管）。
- 合并前必须 rebase 到最新 main 并重跑 CI，**不允许"CI 过了就合"用旧结果**。

### 9.3 Merge Marshal（串行合并）

10 个窗口同时开着 PR，如果谁先绿谁先合，main 会反复被破坏。规则：

| 角色 | 职责 | 在本方案里是谁 |
|---|---|---|
| **Merge Marshal** | 唯一有合并权限的人/Agent；决定合并顺序与时机 | 你（或由你在 Wave 边界授权一个窗口代行） |
| 窗口 | 只负责把自己 PR 弄绿，然后"申请合并" | 各 Agent 窗口 |
| 申请格式 | PR 评论 `@marshal merge-ready wave=2 deps=<已合并的 issue 号>` | — |

Marshal 每次合并后：
1. 等 main 的 CI 变绿；
2. 通知下一顺位窗口 `rebase now`；
3. 若 main 变红，**立即回滚该合并**（revert，不是修），让下一窗口继续——回滚比修快。

---

## 10. 骨架交付自检（第一段的验收清单）

| # | 检查 | 命令 | 结果 |
|---|---|---|---|
| 1 | 后端单测 | `cd backend && python -m pytest` | **16 passed** |
| 2 | 服务启动 | `uvicorn app.main:app --port 8000` | 启动成功，结构化日志输出 |
| 3 | liveness | `curl /health` | `{"status":"ok",...}` |
| 4 | readiness（无依赖时降级） | `curl /ready` | 503 + `database.ok=false`（符合预期：DB 未起） |
| 5 | 10 模块注册 | `curl /openapi.json` | 23 paths，含 10 个 `_status` |
| 6 | 认证链路 | 无 token → 401 / 有 token → 501 | 均正确 |
| 7 | Go Agent 编译 | `cd agent && GOCACHE=/tmp/gocache go build ./...` | 通过；`afc-agent -version` 正常 |
| 8 | 前端依赖 | `cd web && npm install` | 见 §10.1 说明 |
| 9 | 迁移 | `alembic heads` | 单个 head `0001_baseline` |
| 10 | 配置校验 | `APP_ENV=prod pytest tests/test_core.py` | 缺 secret 时报错 |

> **前端冒烟说明**：本机沙箱对 `node` 子进程有资源限制，`npm install` 可能长时间无输出。
> 若前端依赖装不上，不影响 10 个窗口开工（W10 排在 Wave 1 的 I01/I02 只需要 `src/` 下的代码，
> 构建验证可以在本机正常终端里跑一次 `npm install && npm run build`）。

---

## 11. 风险与应对（针对"并行"这个特定玩法）

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 两个窗口改同一文件 | 中 | 中（冲突） | 目录所有权 + 冻结清单 + CI 边界检查 |
| 迁移分叉（两个窗口各建 head） | **高** | 高（必须人工合并迁移） | `alembic heads` 断言 + 命名规范 `w<NN>_<seq>_<slug>` |
| 某窗口卡住阻塞下游 | 中 | 高 | 每个 issue 有"降级实现"：先返回 501 或 mock，让下游按契约先写 |
| Agent 误改其它模块"顺手优化" | **高** | 中 | 边界脚本阻断；PR 模板第一条就是"确认只改了本窗口目录" |
| 60 个 issue 做不完导致放弃 | 中 | 高 | 每个窗口先做 Wave 1 的两个 issue，**4 周做不完就砍 Wave 4**（Wave 4 全是收口，不影响主链路） |
| 文档与代码脱节 | 高 | 中 | 边界、迁移单头、OpenAPI 全部脚本化；`plan/STATUS.md` 每次重建 |
| 上下文污染（Agent 读了别的窗口的计划） | 中 | 中 | 开场 prompt 明确"只读这三个文件" |

---

## 12. 现在就可以开始的命令

```bash
cd /Users/nitai/Documents/WorkCode/Agent/AgentsControl

# 1. 看全局进度
./scripts/window-status.sh

# 2. 开一个窗口（示例 W03）
./scripts/new-window.sh W03

# 3. 进入该窗口并开始第一个未完成 issue
cd .worktrees/w03-task-queue && cat plan/W03.md   # worktree 内是本窗口的完整视图
```

10 个窗口的启动 prompt 见 `plan/PROMPTS.md`，可直接复制给对应 Agent。
