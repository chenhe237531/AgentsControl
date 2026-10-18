#!/usr/bin/env bash
# 为一个窗口准备隔离工作区（worktree + 分支 + handoff 初稿）。
#
# 用法
#   ./scripts/new-window.sh W03
#   ./scripts/new-window.sh W03 --base origin/main
#
# 幂等：重复执行不会报错，只会提示已存在。
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

WIN="${1:-}"
BASE="${3:-origin/main}"
if [[ -z "$WIN" ]]; then
  echo "用法：./scripts/new-window.sh W03 [--base origin/main]" >&2
  exit 2
fi

info=$(python3 - "$WIN" <<'PY'
import sys, yaml, pathlib
plan = yaml.safe_load(pathlib.Path("plan/windows.yaml").read_text(encoding="utf-8"))
wid = sys.argv[1]
for w in plan["windows"]:
    if w["id"] == wid:
        print(w["branch"], w["worktree"], w["name"], sep="|")
        break
else:
    sys.exit(3)
PY
) || { echo "plan/windows.yaml 中没有窗口 $WIN" >&2; exit 2; }

IFS='|' read -r BRANCH WT NAME <<<"$info"

git fetch origin --quiet

if git worktree list | grep -q "$WT"; then
  echo "worktree 已存在：$WT"
  exit 0
fi

if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "本地分支已存在，直接挂载：$BRANCH"
  git worktree add "$WT" "$BRANCH"
else
  # 远端已有同名分支（别人开过/上次中断）则续用，不重建
  if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
    git worktree add "$WT" -b "$BRANCH" "origin/$BRANCH"
  else
    git worktree add "$WT" -b "$BRANCH" "$BASE"
  fi
fi

HANDOFF="$WT/.fleet/handoff/$WIN.md"
if [[ ! -f "$HANDOFF" ]]; then
  mkdir -p "$(dirname "$HANDOFF")"
  cat > "$HANDOFF" <<EOF
## $WIN handoff

- 已完成：无（窗口刚初始化）
- 未完成：$NAME 的第一个 issue
- 坑：无
- 待决策：无
EOF
fi

cat <<EOF

窗口 $WIN 已就绪
  路径   $WT
  分支   $BRANCH
  名称   $NAME
  计划   $WT/plan/$WIN.md

下一步（把这段直接交给该 Agent 窗口）：

  你是 $WIN 窗口。工作目录 $PWD/$WT。
  只读三个文件，不要读其它窗口的计划：
    1. plan/$WIN.md
    2. plan/README.md
    3. .fleet/handoff/$WIN.md
  从第一个状态不是 DONE 的 issue 开始，严格串行；
  每完成一个 issue 立刻 commit + push，并更新 plan/$WIN.md 的状态表。
  改完跑 ./scripts/pr_precheck.sh --local，非 0 不允许提交。
EOF
