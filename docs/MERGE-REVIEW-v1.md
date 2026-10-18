# 合并检查与评审机制 · v1

**日期**：2026-10-18
**配套**：`docs/DEV-PLAN-v1.md`（窗口拆分）、`plan/windows.yaml`（边界与波次）、`scripts/pr_precheck.sh`、`scripts/check_boundaries.py`、`.github/PULL_REQUEST_TEMPLATE.md`
**适用的合并对象**：10 个窗口的全部 PR，无例外（含文档 PR、脚本 PR、迁移 PR）

---

## 0. 这套机制防的是什么

并行开发里，"合并"是最容易被当成走过场的一步。真实失守只有五种，本机制逐条对应：

| 失守方式 | 具体表现 | 对应闸门 |
|---|---|---|
| **越界** | 顺手改了别人模块一行，看起来无害，却让两个窗口互相阻塞 | G0 边界预检（脚本，阻断） |
| **假绿** | 测试通过了，但断言的是 mock 的行为，真实路径根本没跑 | G2 测试有效性（评审，阻断） |
| **契约漂移** | 接口字段悄悄改名，前端在运行时才炸 | G3 契约（OpenAPI diff + 类型生成，阻断） |
| **迁移分叉** | 两个窗口各建一个 head，上线时 upgrade 直接失败 | G3 迁移单头（脚本，阻断） |
| **修不回来** | 合并后 main 红了，越修越乱 | G5 可回滚性 + 熔断回滚（流程，阻断） |

一条总原则：

> **能脚本化的判断绝不交给人，交给人只做"这条改动是否值得存在"的判断。**

---

## 1. 角色与权限

| 角色 | 权限 | 不能做什么 | 本项目中是谁 |
|---|---|---|---|
| **窗口 Agent** | 在自己 worktree 里写代码、push 到 `win/*` 分支、开/更新 Draft PR、贴证据 | 不能合并、不能改冻结文件（除非带 `x-shared-registry`）、不能改别人的 `plan/Wxx.md` | 10 个窗口 |
| **对抗评审 Agent** | 只读仓库 + 读 PR diff，输出评审结论与证据行号 | 不能改代码、不能合并 | 每次评审新起一个干净上下文（不复用窗口上下文） |
| **Merge Marshal** | 唯一合并权；排序、rebase 指令、回滚 | 不能替窗口改业务代码；不能跳过任何闸门 | 你，或你在波次边界临时授权的窗口 |
| **你（Owner）** | 最终放行/否决；处理例外 | 不做逐行 style 评审（那是脚本和评审 Agent 的事） | — |

**为什么评审必须换一个上下文**：同一个 Agent 刚写完代码，再看自己的代码只会确认自己是对的。
评审 Agent 的价值来自"不知道你为什么这么写"，所以它必须只读 PR 与验收标准。

---

## 2. 六道闸（G0–G5）

### G0 边界预检（自动，阻断）

```bash
scripts/pr_precheck.sh                  # 本地，提交前必跑
python scripts/check_boundaries.py --changed --base origin/main
```

| 检查 | 阻断条件 |
|---|---|
| 改动文件是否全在本窗口 `allow` 内 | 有一个越界即阻断 |
| 是否改冻结文件且无 `x-shared-registry` 标签 | 阻断 |
| 冲突标记 `<<<<<<<` / `>>>>>>>` | 阻断 |
| `.env` / `*.pem` / `*.key` 入库 | 阻断 |
| 迁移是否单头、是否都有 `downgrade()` | 阻断 |

### G1 可构建（自动，阻断）

后端 `pytest` 全绿、`ruff check` 无 error；前端 `tsc --noEmit` + `next build`；Go `gofmt` 无差异 + `go vet` + `go build`。
**任何一项红灯都不允许进入人工评审**——人工评审的时间只花在机器判断不了的事上。

### G2 测试有效性（人工/评审 Agent，阻断）

只问三个问题，每个都要证据：

1. 新增的行为，是否有**会失败的测试**？（把断言删掉，测试还能过 → 无效）
2. 测试是否打在**真实边界**上？（状态机是否覆盖非法流转；ingest 是否覆盖重复/乱序/缺口）
3. 是否为了通过测试而**放宽了断言**？（`assert x is not None` 这种不算验证）

### G3 契约与迁移（自动 + 人工，阻断）

| 对象 | 规则 |
|---|---|
| OpenAPI 快照 | 有 diff 必须在 PR 描述里逐条说明"谁受影响、谁改"；无说明阻断 |
| 前后端类型 | 后端字段改名 → `web` 的 typecheck 必须失败；失败被"手动改类型"绕过则阻断 |
| 迁移 | 只增不改；单头；upgrade → downgrade → upgrade 三连通过；大表加列必须允许 NULL 或有默认值 |
| 设备协议 | `agent/internal/protocol/types.go` 变更必须 W04 + W05 同 PR，附对拍测试 |

### G4 安全与权限（人工/评审 Agent，阻断）

| 检查项 | 反例（出现即阻断） |
|---|---|
| 认证 | 新增端点没挂 `OwnerDep` 或 scope |
| 授权 | 用"前端隐藏按钮"代替后端校验 |
| 输入 | 直接 `eval` / 拼接 shell / 未校验的 `order_by` 字段 |
| 秘密 | 密钥出现在代码、日志、错误信息、URL |
| 令牌 | 存 localStorage、放 query string、日志里出现完整 token |
| 设备 | 第三方设备（`trust=third_party`）能收到指令 |

### G5 可运维性（人工/评审 Agent，阻断）

| 检查项 | 通过标准 |
|---|---|
| 可观测 | 关键路径有结构化日志与计数；失败有明确 `code` |
| 健康 | 新增外部依赖必须进 `/ready`（或显式说明为什么不算就绪条件） |
| 回滚 | 能只 revert 该 PR 回到上一状态；迁移可 downgrade |
| 幂等 | 定时任务/重试路径重复执行结果一致 |
| 静默失败 | 不允许 `except: pass`；不允许丢弃事件不计数 |

---

## 3. Merge-Ready 定义（12 条，缺一不可）

一个 PR 只有同时满足以下 12 条，才可以贴 `merge-ready`：

- [ ] 1. 只改了本窗口 `allow` 内的文件（`pr_precheck.sh` 输出已贴进 PR）
- [ ] 2. 所有 commit 带 `(#issue)` 且能对应到 `plan/windows.yaml` 里的 issue
- [ ] 3. CI 六路 job 全绿（非"部分绿"）
- [ ] 4. 新增/修改行为有会失败的测试，且测试名能看出测什么
- [ ] 5. 迁移：单头、可 downgrade、已在干净库上跑过三连
- [ ] 6. OpenAPI 快照 diff 已在描述里解释（或明确"无 diff"）
- [ ] 7. PR 描述里逐条勾选该 issue 在 `plan/Wxx.md` 中的验收标准
- [ ] 8. 对抗评审结论为 PASS（评分 ≥ 3.5/5，且无阻断项）
- [ ] 9. 分支已 rebase 到**当前** main，并在 rebase 后重跑过 CI
- [ ] 10. `plan/Wxx.md` 状态已更新为 REVIEW/DONE，`handoff` 已写"改了什么/没改什么/下一步/坑"
- [ ] 11. 不含"顺手优化"（格式化全仓、重命名无关变量、升级无关依赖）
- [ ] 12. 若动过冻结文件，PR 带 `x-shared-registry` 且不含业务代码

---

## 4. 合并流程（Marshal 视角，严格串行）

```
① 窗口 push → Draft PR（标题 [W03] 任务队列与租约）
        │
② 本地 pr_precheck.sh 通过 → 贴输出到 PR → 勾完 §3 的 12 条
        │
③ 评论: @marshal merge-ready wave=2 deps=#41,#42
        │
④ Marshal 检查三件事：
     a. 该 issue 在 windows.yaml 的 depends_on 是否都已合并
     b. 同波次内是否有更早的 PR 未合（有则排队，不插队）
     c. 是否在"禁区时间"（周五 15:00 后 / 你不在电脑前）
        │
⑤ 触发对抗评审 Agent（新上下文，只给 PR diff + 验收标准）
     → 输出：评分表 + 阻断项 + 证据行号
        │
⑥ PASS 后：Marshal 要求 rebase → 重跑 CI → 绿后 squash merge（1 个 commit）
        │
⑦ 合并后 5 分钟内跑 scripts/post-merge.sh：
     alembic upgrade head（staging）→ /ready=200 → 关键端点 smoke
        │
⑧ main 绿 → 广播 "rebase now" 给同波次其余窗口 → 进入下一顺位
        │
⑨ 每合并 3 个 PR 或每 2 小时，重建 plan/STATUS.md 并留档
```

**为什么必须串行**：10 个 PR 同时绿、谁先合都行，会导致 main 每 30 分钟变红一次，
而且红的时候你无法判断是哪一个 PR 引起的。串行的代价是排队，收益是"每次只有一个嫌疑人"。

**为什么 squash**：窗口分支是草稿区（允许 WIP commit），进 main 的必须是可读的一个提交。
`squash` 后 commit 标题保持 `feat(#42): xxx`，保证 `window-status.sh` 还能解析进度。

---

## 5. 评审评分表（对抗评审 Agent 输出格式）

每项 1–5 分，**任何一项 ≤2 分即阻断**，总分 <3.5 阻断。

| # | 维度 | 5 分标准 | 1 分标准 |
|---|---|---|---|
| 1 | 范围 | 只做 issue 说的事，改动可逐行解释 | 夹带无关重构 |
| 2 | 分层 | controller 无业务逻辑、service 不碰 HTTP、多步写入在事务里 | 逻辑全塞 router |
| 3 | 测试 | 覆盖非法路径与边界，断言具体值 | 只有 happy path、断言 `is not None` |
| 4 | 契约 | 字段/错误码/迁移完全符合 CONTRACT-v1 | 悄悄改字段或错误码 |
| 5 | 安全 | 认证/授权/校验齐全，无秘密泄漏 | 端点裸奔或拼接 shell |
| 6 | 可运维 | 日志/计数/健康/回滚都到位 | 静默失败、无日志 |

评审结论必须包含：

```yaml
verdict: PASS | BLOCK
score: 4.2
blocking:
  - file: backend/app/modules/tasks/service.py
    line: 118
    why: 回收租约时未在事务内更新 attempt，并发下会重复计数
    fix: 用 with transaction(): 包裹 UPDATE ... RETURNING
non_blocking:
  - 建议把 90s 写成常量（配置化留给 W01）
evidence:
  - pytest tests/test_tasks.py::test_lease_reclaim 未断言 attempt 递增
```

**"证据"是硬要求**：没有文件 + 行号的结论视为无效评审，Marshal 不得据此放行。
这条是为了防止评审 Agent 用"看起来不错"糊过去。

---

## 6. 波次放行（Wave Gate）

每个 Wave 结束不是"时间到了"，而是"这批断言成立"：

| Wave | 放行断言（必须能演示） |
|---|---|
| 1 | 无 token → 401；登录 → 刷新 → 旧刷新令牌失效；迁移单头；`/ready` 语义正确 |
| 2 | 收件箱→三问→目标卡；任务 claim→租约→回收；设备心跳→suspect→offline；三问页可点 |
| 3 | 真机三通道上报；worker claim→complete；账本能出当日 CPO；带宽熔断可触发 |
| 4 | 模型雷达出图且有标红维度；甘特图能标出撞车/阻塞/偏离；E2E 冒烟通过 |
| 5 | 一个真实 issue 从认领到 PR merged 的 verdict 全链路可追溯；staging 可部署可回滚 |

放行方式：Marshal 在 `plan/STATUS.md` 顶部写一行 `Wave N: PASS (日期)`，
并把该波所有 PR 的 label 从 `wave:N` 改为 `wave:N-done`。**没有这行，下一波不许合并。**

---

## 7. 冲突处置（三类热点，三种处理）

| 场景 | 处理 | 责任 |
|---|---|---|
| **两人改了同一文件**（边界脚本未拦住，如共享 `docs/`） | 后合并的一方 rebase 并解决；解决后必须在 PR 里说明"如何验证没丢对方改动" | 后合并方 |
| **迁移多头** | 后到者 `alembic merge -m "merge w03/w05" heads`，生成空迁移；**禁止删对方的迁移文件** | 后到者 |
| **OpenAPI 快照冲突** | 快照是生成物，**永远重生成**而不是手工合并：`python scripts/openapi_snapshot.py` | 后到者 |
| **冻结文件被两人同时需要改** | 撤回各自的改动，合并成一个 `x-shared-registry` PR，一次性改完 | Marshal 指定 |

**热点文件的"预约制"**：需要改 `backend/app/core/**` 时，先在 PR 评论里写 `lock: core`，
Marshal 确认无人占用后再动手。一次只允许一个窗口持有该锁。

---

## 8. 熔断与回滚

| 触发 | 动作 | 恢复条件 |
|---|---|---|
| 同一 PR CI 连败 3 次 | 熔断：转人工，禁止自动重试 | 人工定位后重新开分支 |
| main 合并后变红 | **立即 revert 该 merge**（不修） | revert 后 main 绿，原 PR 重新提交 |
| staging 部署后 `/ready` 失败 | 回滚到上一镜像 tag（`docker compose up -d` 指定 tag） | 修复后重新部署 |
| 迁移导致数据异常 | 停 API → `alembic downgrade -1` → 从 COS 恢复当日 dump | 演练记录写入 RUNBOOK |

**回滚优先于修复**，理由很直接：main 红着的时候，其余 9 个窗口都在浪费 CI 时间与注意力；
revert 是 1 分钟，debug 可能是一下午。

**回滚也必须走 PR**（`revert: #<PR>`），不允许直接 push main——否则"什么时候回滚的、为什么"
会永久丢失，而这恰恰是复盘最需要的信息。

---

## 9. 对抗评审 Agent 的启动 prompt（直接用）

```
你是本次合并的对抗评审。你**没有参与**这次实现，也不要假设实现是对的。

输入：
  - PR diff（git diff origin/main...HEAD）
  - 该 issue 的验收标准（plan/Wxx.md 中对应条目）
  - 边界：plan/windows.yaml 中该窗口的 allow 列表

任务：
  1. 按 docs/MERGE-REVIEW-v1.md §5 的 6 个维度打分（1–5），给出总分
  2. 逐条核对验收标准，标出"未满足/无法验证/满足"
  3. 找出至少 3 个可能导致线上故障的具体问题（找不到就说明你检查了哪些路径）
  4. 每个结论必须带 文件:行号；没有行号的结论视为无效
  5. 只输出 yaml 结论块（§5 的格式），不要写鼓励性评价

禁止：修改代码、给"建议重构"这类无法验证的意见、评价代码风格（ruff 已管）。
```

---

## 10. 例外通道（Hotfix）

只允许一种情况绕过波次：**生产不可用或数据错误**。

```
1. 从 main 切 hotfix/<slug>，不经过窗口分支
2. 改动范围 ≤ 50 行，且只修故障本身
3. 必须带一条回归测试（否则下次还会坏）
4. Marshal 自己评审（不能再起对抗评审，时间不允许），但要在 PR 里写明"跳过了哪道闸、为什么"
5. 合并后 24 小时内补一份 docs/postmortem/<date>-<slug>.md（根因 + 为什么没被测试发现）
```

**没有 postmortem 的 hotfix 不计入完成**——否则同一个坑会踩第二次。

---

## 11. 度量（每周复盘只看这 4 个数）

| 指标 | 目标 | 恶化说明什么 |
|---|---|---|
| main 红着的总时长 | < 30 min/周 | 串行合并被破坏，或闸门被绕过 |
| merge-ready → merged 的中位时长 | < 2 h | 评审或排期成为瓶颈 |
| 首轮评审通过率 | ≥ 60% | 窗口没读验收标准，或边界不清 |
| 越界改动次数 | 0 | 边界表有缺口，需要补 allow 或冻结 |

第 4 项只要出现一次，就必须回到 `plan/windows.yaml` 补规则，而不是"提醒一下注意"。

---

## 12. 命令清单（复制即用）

```bash
# 提交前（窗口）
scripts/pr_precheck.sh

# 边界与迁移自检
python scripts/check_boundaries.py --changed --base origin/main
cd backend && alembic heads

# 申请合并（在 PR 评论）
@marshal merge-ready wave=2 deps=#41,#42

# 合并后验证（Marshal）
scripts/post-merge.sh

# 回滚（Marshal）
gh pr create --title "revert: #<PR>" --body "revert 原因..." --base main
```

> 本机制的唯一目的：**让"合并"这件事在 10 个窗口并行时仍然是可预测的。**
> 任何一条规则如果执行两周后仍带来痛苦，就改规则；但不要在执行中临时绕过——
> 绕过一次，边界表就失效一次，后面所有窗口都会开始怀疑它。
