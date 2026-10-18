#!/usr/bin/env bash
# 合并后验证（Merge Marshal 专用）。
#
# 目的：合并进 main 只是"代码进了主干"，不等于"主干还能跑"。
# 本脚本只做只读验证 + 一条可选迁移，不修改业务数据。
#
# 用法
#   scripts/post-merge.sh                          # 纯静态验证（不需要 DB）
#   scripts/post-merge.sh --with-db                # 额外跑 alembic upgrade head（需要 DATABASE_URL）
#   API_BASE=http://localhost:8000 scripts/post-merge.sh --with-db   # 额外探活
#
# 退出码 0 通过 / 1 失败（失败即按 MERGE-REVIEW §8 回滚，不在 main 上修）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
WITH_DB=0
[[ "${1:-}" == "--with-db" ]] && WITH_DB=1

FAIL=0
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*" >&2; FAIL=1; }
warn() { printf '  WARN  %s\n' "$*"; }

step "1/6 工作区与分支"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$BRANCH" == "main" ]] && ok "在 main（$BRANCH）" || warn "当前分支是 $BRANCH，不是 main"
if [[ -n "$(git status --porcelain)" ]]; then
  warn "工作区有未提交改动（合并后验证应基于干净工作区）"
else
  ok "工作区干净"
fi

step "2/6 同步远端"
git fetch -q origin main && ok "已 fetch origin/main" || bad "fetch 失败"

step "3/6 边界与迁移静态检查"
if "$PYTHON" scripts/check_boundaries.py >/tmp/afc-boundary.log 2>&1; then
  ok "边界检查通过（10 窗口 / 60 issue）"
else
  tail -20 /tmp/afc-boundary.log
  bad "边界检查失败（见上方输出）"
fi
HEADS=$(cd backend && "$PYTHON" -m alembic heads 2>/dev/null | wc -l | tr -d ' ')
[[ "$HEADS" == "1" ]] && ok "迁移单头" || bad "迁移 head 数量为 $HEADS（应为 1）"

step "4/6 后端单测 + OpenAPI 契约"
if (cd backend && "$PYTHON" -m pytest -q >/tmp/afc-pytest.log 2>&1); then
  ok "pytest 通过：$(grep -E 'passed' /tmp/afc-pytest.log | tail -1)"
else
  tail -20 /tmp/afc-pytest.log
  bad "pytest 失败"
fi
if "$PYTHON" scripts/openapi_snapshot.py --check >/tmp/afc-openapi.log 2>&1; then
  ok "OpenAPI 快照无漂移"
else
  warn "OpenAPI 快照有变化（契约变更需在 PR 中解释）："
  sed -n '1,15p' /tmp/afc-openapi.log
fi

step "5/6 迁移执行（可选）"
if [[ "$WITH_DB" == "1" ]]; then
  if (cd backend && "$PYTHON" -m alembic upgrade head); then
    ok "alembic upgrade head 完成"
  else
    bad "迁移失败 —— 立即回滚该合并"
  fi
else
  warn "跳过（加 --with-db 启用）"
fi

step "6/6 探活（可选）"
if [[ -n "${API_BASE:-}" ]]; then
  CODE=$(curl -s -o /tmp/afc-ready.json -w '%{http_code}' --noproxy '*' "$API_BASE/ready" || echo 000)
  if [[ "$CODE" == "200" ]]; then
    ok "/ready 200：$(cat /tmp/afc-ready.json)"
  else
    bad "/ready 返回 $CODE（预期 200）"
  fi
else
  warn "跳过（设置 API_BASE 启用）"
fi

printf '\n'
if [[ "$FAIL" == "0" ]]; then
  echo "合并后验证通过。下一步："
  echo "  1) 广播 rebase now 给同波次其余窗口"
  echo "  2) 把本 PR 对应 issue 在 plan/Wxx.md 标为 DONE"
  echo "  3) ./scripts/window-status.sh --write 重建看板"
  exit 0
fi
echo "合并后验证失败 —— 按 docs/MERGE-REVIEW-v1.md §8 处理："
echo "  git revert <merge commit> && push   （先让 main 绿，修复在分支上做）"
exit 1
