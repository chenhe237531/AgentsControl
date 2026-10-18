# Agent Fleet Control

用 500 个 Agent 放大一个人的带宽。本仓库是**控制面**：任务、预算、验收、账本。

> 执行面（Orca / 编码 Agent / 设备）只作为"领活-交货"的黑盒，不由本仓库实现。
> 架构依据：`docs/TECH-ARCHITECTURE-v1.md`；产品依据：`docs/PRODUCT-DESIGN-v0.2.md`。

## 仓库结构

```
backend/            FastAPI 单体（唯一后端）+ SQLAlchemy 2.0 + Alembic + PostgreSQL
  app/core/         配置 / 日志 / 错误 / 数据库 / Redis / 安全原语   ← W01
  app/api/          集中路由注册（冻结）+ 健康检查
  app/modules/      10 个业务模块，每模块归属一个窗口（见各自 README）
  migrations/       Alembic 迁移（单头纪律，见 docs/DEV-PLAN-v1.md §8）
  tests/            单测（不依赖真实 PG/Redis）
web/                Next.js 15 控制台（10 个页面，全部路由已在骨架期占位）
agent/              Go 设备端（三通道上报：心跳 60s / 增量 30s / 快照 60min）
deploy/             Docker Compose + Caddy（自动 HTTPS）+ 本地依赖编排
docs/               产品、架构、开发计划、窗口任务书
plan/               窗口与 issue 的机器可读清单（边界 / 依赖 / 进度）
scripts/            骨架生成、窗口状态、PR 预检、续跑
```

## 快速开始

```bash
# 1) 依赖
docker compose -f deploy/docker-compose.dev.yml up -d      # PG + Redis
cp .env.example .env                                        # 填本地值，勿提交

# 2) 后端
cd backend && python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000
# curl localhost:8000/health → {"status":"ok",...}

# 3) 前端
cd web && npm install && npm run dev   # localhost:3000

# 4) 设备端
cd agent && go build ./... && AFC_API_BASE_URL=http://localhost:8000/api/v1 \
  AFC_NODE_ID=n-dev AFC_TOKEN=dev-token go run ./cmd/agent
```

## 开发方式（重要）

开发按 **10 个窗口 × 若干 Issue** 推进，**一个 Issue = 一个 worktree = 一个分支 = 一个 PR**。
动手前必读：

1. `docs/DEV-PLAN-v1.md` —— 顺序、边界、波次、合并规则
2. `plan/Wxx.md` —— 你所在窗口的 issue 顺序与验收
3. `plan/boundaries.yaml` —— 你能改哪些文件（越界会被 PR 预检拦下）
4. `.fleet/handoff/Wxx.md` —— 上一个 issue 留下的交接（≤10 行）

```bash
# 开始/恢复一个窗口
scripts/window-resume.sh W03
# 看进度
scripts/window-status.sh
```

## 硬约束

- 配置全部来自环境变量，启动即校验；真实密钥只存在于 `.env`（已 gitignore）
- 令牌不进 localStorage、不进 URL；设备 token 只存哈希
- 迁移只增不改，合并前必须单头（`alembic heads` 只输出 1 行）
- 共享文件（`app/api/router.py`、`web/src/config/nav.ts`、`ci.yml` 等）冻结，
  修改必须走 `x-shared-registry` 变更请求
- 控制器不写业务逻辑；服务层不依赖 Request/Response；多步写入走事务
