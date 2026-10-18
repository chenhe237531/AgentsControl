# plan/README.md · 10 窗口并行开发的运行手册

> 本文件是**所有窗口共享**的约定。窗口开新对话时读三样东西：
> `plan/W<NN>.md`（自己的任务）、本文件（全局规则）、`.fleet/handoff/W<NN>.md`（上次交接）。
> **不要读其它窗口的计划**——既浪费上下文，也容易误改别人的文件。

---

## 1. 一页速览

| 你要做的事 | 命令 / 位置 |
|---|---|
| 看全局进度 | `./scripts/window-status.sh` |
| 首次创建自己的 worktree | `./scripts/new-window.sh W03` |
| 提交前自检 | `./scripts/pr_precheck.sh --local` |
| 查自己的 issue 顺序 | `plan/W03.md` |
| 交接 | `.fleet/handoff/W03.md`（≤10 行） |
| 申请合并 | PR 评论 `@marshal merge-ready wave=2 deps=W01-I01` |
| 改冻结文件 | 开 issue + label `x-shared-registry`，单独 PR |
| 加/改配置项、加模块 | 同上（属于契约变更） |

---

## 2. 分支与目录约定

```
分支      win/w<NN>-<slug>          例：win/w03-task-queue
worktree  .worktrees/w<NN>-<slug>   例：.worktrees/w03-task-queue
提交      <type>(#<issue>): <描述>   例：feat(#42): 实现租约回收
PR        每窗口一个长期 Draft PR，标题 [W03] 任务队列与租约
```

`type` 只能是：`feat` `fix` `chore` `test` `docs` `refactor` `perf` `build` `ci`。

**为什么一个窗口只开一个 PR**：issue 之间是串行依赖，每个 issue 单开 PR 会制造 60 个分支和 60 次 rebase；
一个长期分支 + 每 issue 若干提交，进度一样可追踪（脚本按提交消息里的 issue 号解析），
但只有 10 个分支需要维护。

---

## 3. 每个 issue 的固定六步

```
1. plan/W<NN>.md 里把该 issue 状态改成 DOING
2. git pull --rebase origin main        ← 只在 issue 开始时做一次
3. 写代码 + 写测试 + ./scripts/pr_precheck.sh --local
4. 刷新 .fleet/handoff/W<NN>.md（≤10 行）
5. commit + push（允许 WIP，但必须能编译）
6. 更新 PR 描述 + plan/W<NN>.md 状态改为 REVIEW / DONE
```

**小步提交的底线**：每个 commit 都必须能通过 `gate`。
如果做不到（比如中间状态要改两处），就先把两处一起提交，而不是提交一个坏状态。

---

## 4. 文件边界（这是防冲突的唯一有效手段）

### 4.1 你只能改 `plan/windows.yaml` 里 `allow` 列出的路径

越界会被 CI 阻断（`scripts/check_boundaries.py --changed`）。这是**故意的**：
一旦允许"顺手改一下"，10 个窗口的改动就会像藤蔓一样互相缠住，合并变成手工劳动。

### 4.2 冻结文件

| 文件 | 谁能改 | 怎么改 |
|---|---|---|
| `backend/app/api/router.py` | 无人（只在波次边界） | shared-registry 变更请求 |
| `web/src/config/nav.ts` | 同上 | 同上 |
| `backend/app/core/**` | W01 | shared-registry（W01 例外可自查自合） |
| `agent/internal/protocol/types.go` | W04 + W05 双方 | **同一个 PR 里同时改两端** |
| `plan/windows.yaml`、`plan/boundaries.yaml` | Marshal | 只在波次边界 |
| `.github/workflows/ci.yml` | W09 | 且不能放松任何检查 |

### 4.3 跨模块读依赖

只允许两种方式：① 调用对方模块 `service.py` 的公开函数；② 只读 SQL（不许 INSERT/UPDATE 别人的表）。
需要对方提供新接口时，写在自己的 issue 描述里（如"W03-I04 需要 `GET /ledger/budget-status`"），
由对方在自己计划里排——**不要自己动手在对方目录里加**。

---

## 5. 中断与续跑

### 5.1 三种中断，三种恢复

| 情形 | 恢复动作 | 恢复成本 |
|---|---|---|
| 对话被关掉，代码已提交 | 新对话读 `plan/W<NN>.md` → 找第一个非 DONE 的 issue → 看 `handoff` 的"未完成" | < 5 分钟 |
| 改了没提交（崩溃/断电） | `git status` 看残留 → 提交为 WIP → 继续 | < 10 分钟 |
| 做了半个 issue 发现方向错 | 在 `handoff` 写清"为什么错" → 回退到该 issue 起点 commit | 半个 issue |

### 5.2 handoff 规范（≤10 行，多一行都不写）

```markdown
## W03 handoff
- 已完成：W03-I01 任务状态机（a1b2c3d）
- 未完成：W03-I02 租约回收，已做完 lease 接口，回收脚本没写
- 坑：UPDATE ... RETURNING 在 asyncpg 下要显式 commit，否则租约看起来"回收了"其实没落库
- 待决策：租约时长 90s 是否要按设备类型区分（笔记本 vs VPS）
```

**为什么限制 10 行**：handoff 越长越没人更新，最后变成一份过期文档。
它的唯一用途是"让下一个接手者在 5 分钟内知道该干什么"，不是写周报。

### 5.3 进度是算出来的，不是记出来的

`plan/STATUS.md` 由 `./scripts/window-status.sh --write` 从 git log + gh PR 状态重建。
**不要手工编辑它**——手工维护的状态在第三天就会和现实脱节。

---

## 6. 合并

详见 `docs/MERGE-REVIEW-v1.md`。三条最容易违反的：

1. **只有 Merge Marshal 能合**。窗口自己点合并按钮 = 绕过顺序控制，main 会反复被破坏。
2. **同一波次按依赖顺序合**，同一模块的 issue 必须串行。
3. **合并前必须 rebase 到最新 main 并重跑 CI**，不能用几分钟前的绿灯结果。

---

## 7. 沟通约定（写进文件，不要只写在对话里）

| 情况 | 做法 |
|---|---|
| 需要别人配合 | 在 issue 里写清接口，@ 对应窗口，**不要私聊**（私聊会丢） |
| 发现边界设计有问题 | 开 `x-shared-registry` issue，附"为什么现有边界不可行" |
| 发现别人的 bug | 开 issue 指派给对应窗口，不要顺手改 |
| 卡住超过 2 小时 | 在 handoff 里记 `BLOCKED` + 原因，跳过该 issue 做下一个（串行不等于死等） |

---

## 8. 常见错误（每一条都真实发生过）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 一个 issue 里顺便重构了别的模块 | 冲突 + 评审无法判断改动意图 | 重构单独开 issue，或直接放弃（能跑就别动） |
| 迁移文件用随机 hash 命名 | 十个窗口的迁移无法判断顺序 | `w<NN>_<seq>_<slug>.py` |
| 忘了 `alembic upgrade` 就提交 | 队友拉代码后表不存在 | 提交前跑一次 `pytest`（CI 会跑迁移 roundtrip） |
| handoff 写"基本完成" | 接手者不知道还差什么 | 写具体："lease 接口完成，回收脚本未写" |
| PR 里堆了 5 个 issue 的改动 | 无法按波次挑着合 | 一个 issue 一组提交，PR 描述里列清单 |
| 把 token 写进代码临时调试 | 密钥泄露 | 用 `.env`，且 `.env` 已在 `.gitignore` |
