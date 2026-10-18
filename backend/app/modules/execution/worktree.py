"""worktree 生命周期 —— 任务的物理载体（磁盘上的事实）。

契约（TECH-ARCHITECTURE-v1.md §5.2）：
- 一任务一分支一 worktree：目录 `.worktrees/<issue>-<slug>`，分支 `issue/<issue>-<slug>`；
- 创建时写 `.fleet-task.json`（任务自描述：目标/预算/验收命令/版本，防重派 idem_key）；
- 回收前检查未提交改动，有改动拒绝并返回文件清单——**Agent 的工作不允许被静默丢弃**；
- 只依赖 git 本身（稳定事实），Orca CLI 只是住在 worktree 里的房客。

单测用临时 git 仓库，绝不碰真实仓库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 30.0
FLEET_TASK_FILENAME = ".fleet-task.json"


class WorktreeError(Exception):
    """worktree 操作失败（对外 409/500，消息已可读）。"""


class WorktreeExistsError(WorktreeError):
    """worktree 目录已存在：重派不允许产生第二个 worktree。"""


class GitCommandError(WorktreeError):
    """git 命令失败：带命令与 stderr，便于排障。"""


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    issue: str
    slug: str
    path: str
    branch: str
    base_ref: str | None


async def _git_rc(args: list[str], *, cwd: str | Path) -> tuple[int, str, str]:
    """执行 git 命令，返回 (rc, stdout, stderr)，不抛错（供探查类调用）。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        return -1, "", f"git {args[0]} 超时（{GIT_TIMEOUT_SECONDS}s）于 {cwd}"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _git(args: list[str], *, cwd: str | Path) -> str:
    """执行 git 命令，失败即抛 GitCommandError（stderr 是排障唯一入口）。"""
    rc, stdout, stderr = await _git_rc(args, cwd=cwd)
    if rc != 0:
        raise GitCommandError(f"git {' '.join(args)} 失败（rc={rc}）：{stderr.strip()[:300]}")
    return stdout


def fleet_task_idem_key(task_id: str, path: str) -> str:
    """防重派键：任务与 worktree 位置的联合摘要，写进 .fleet-task.json。"""
    return hashlib.sha1(f"{task_id}:{path}".encode()).hexdigest()[:16]


class WorktreeManager:
    def __init__(self, repo_root: str | Path, worktrees_dir: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktrees_dir = (
            Path(worktrees_dir).resolve()
            if worktrees_dir is not None
            else self.repo_root / ".worktrees"
        )

    # ------------------------------------------------------------ 创建

    async def create(
        self,
        *,
        issue: str,
        slug: str,
        base_ref: str | None = None,
        fleet_task: dict[str, Any] | None = None,
    ) -> WorktreeInfo:
        name = f"{issue}-{slug}"
        path = self.worktrees_dir / name
        if path.exists():
            raise WorktreeExistsError(
                f"worktree 已存在：{path}（重派请复用原会话，或先 DELETE 回收）"
            )
        branch = f"issue/{name}"
        rc, _, _ = await _git_rc(
            ["show-ref", "--verify", f"refs/heads/{branch}"], cwd=self.repo_root
        )
        if rc == 0:
            # 分支已存在（回收后重派）：复用分支与既有提交，base_ref 不再生效
            await _git(["worktree", "add", str(path), branch], cwd=self.repo_root)
        else:
            args = ["worktree", "add", "-b", branch, str(path)]
            if base_ref:
                args.append(base_ref)
            await _git(args, cwd=self.repo_root)
        info = WorktreeInfo(issue=issue, slug=slug, path=str(path), branch=branch, base_ref=base_ref)
        if fleet_task:
            self._ensure_fleet_excluded()
            self.write_fleet_task(str(path), fleet_task)
        return info

    def _ensure_fleet_excluded(self) -> None:
        """.fleet-task.json 是控制面元数据，不是 Agent 的工作：进 .git/info/exclude，
        让它不算未提交改动（否则每个新会话都是"脏"worktree，干净回收必被拒）。"""
        git_dir = self.repo_root / ".git"
        if not git_dir.is_dir():  # repo_root 本身是 worktree 等：找不到主仓 exclude，跳过
            return
        exclude = git_dir / "info" / "exclude"
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if FLEET_TASK_FILENAME in lines:
            return
        lines.append(FLEET_TASK_FILENAME)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_fleet_task(self, path: str, payload: dict[str, Any]) -> Path:
        """写 .fleet-task.json：任务自描述，Agent 在 worktree 里能读到自己是谁。"""
        file_path = Path(path) / FLEET_TASK_FILENAME
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return file_path

    def read_fleet_task(self, path: str) -> dict[str, Any]:
        file_path = Path(path) / FLEET_TASK_FILENAME
        if not file_path.exists():
            return {}
        return json.loads(file_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 检查与回收

    async def uncommitted_files(self, path: str) -> list[str]:
        """未提交改动清单（git status --porcelain）；干净返回 []。"""
        out = await _git(["status", "--porcelain"], cwd=path)
        files: list[str] = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            entry = line[3:] if len(line) > 3 else line
            entry = entry.split(" -> ")[-1].strip('"')
            if entry:
                files.append(entry)
        return files

    async def diff_numstat(self, path: str, *, base_ref: str) -> str:
        """harvest 用：分支对基线的 numstat（三路点，容忍基线前移）。"""
        return await _git(["diff", "--numstat", f"{base_ref}...HEAD"], cwd=path)

    async def remove(self, path: str, *, force: bool = False) -> None:
        """删除 worktree。force=True 才允许丢弃未提交改动。"""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        await _git(args, cwd=self.repo_root)


def default_manager() -> WorktreeManager:
    """生产装配：仓库根与 worktree 根来自环境变量（core 配置冻结，不越界加项）。"""
    repo_root = os.getenv("FLEET_REPO_ROOT", os.getcwd())
    worktrees_dir = os.getenv("FLEET_WORKTREES_DIR")
    return WorktreeManager(repo_root, worktrees_dir)
