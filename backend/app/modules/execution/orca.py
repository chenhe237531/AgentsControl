"""Orca 适配（浅接口）——探测与六个原子动作的入口。

纪律（INTEGRATION-ORCA.md §5）：
- Orca 没有公开 HTTP API/SDK，适配只允许走 CLI 子进程；
- 接口做浅：本文件只封装命令调用与输出解析，语义（会话=租约、产物引用、
  事件流）都在 service 层；
- 探测失败 → available=false → 拒绝派 Orca 类任务，**不猜**。

探测策略（plan/W06.md I02 验收 ④）：
- `probe()` 只在首次访问（冷启动）与每 10 分钟的刷新循环里执行；
- 结果进 Redis（TTL 10 分钟），HTTP 请求只读缓存；
- 版本输出格式变化 → 明确错误「Orca CLI 输出格式变了」，绝不空指针。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.redis import cache_get, cache_set

logger = logging.getLogger("afc.execution.orca")

PROBE_CACHE_KEY = "execution:orca:probe"
PROBE_TTL_SECONDS = 600  # 10 分钟：探测结果的缓存与刷新周期
CLI_TIMEOUT_SECONDS = 5.0

# 测试里置 False，避免刷新协程悬挂在 TestClient 的事件循环上
REFRESHER_ENABLED = True

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?[-\w.]*)")


class OrcaError(Exception):
    """Orca 适配层失败（消息已可读，service 层负责翻译成 HTTP 语义）。"""


class OrcaFormatError(OrcaError):
    """Orca CLI 输出与约定格式不符（升级/换发行版时会触发）。"""


# ---------------------------------------------------------------- 子进程执行缝

@dataclass(frozen=True, slots=True)
class RunnerResult:
    rc: int
    stdout: str
    stderr: str


class Runner(Protocol):
    async def run(self, args: list[str], *, timeout: float = CLI_TIMEOUT_SECONDS) -> RunnerResult: ...


class CliRunner:
    """真实实现：asyncio 子进程。测试里用 FakeRunner 顶替，不在 CI 真跑 Orca。"""

    async def run(self, args: list[str], *, timeout: float = CLI_TIMEOUT_SECONDS) -> RunnerResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except (FileNotFoundError, PermissionError) as exc:
            return RunnerResult(rc=127, stdout="", stderr=str(exc))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            return RunnerResult(rc=-1, stdout="", stderr=f"timeout after {timeout}s")
        return RunnerResult(
            rc=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def default_worktree_root() -> str:
    """worktree 根目录：环境变量优先，其次仓库根下的 .worktrees/。"""
    root = os.getenv("FLEET_REPO_ROOT", os.getcwd())
    return os.getenv("FLEET_WORKTREES_DIR", os.path.join(root, ".worktrees"))


# ---------------------------------------------------------------- 探测结果

@dataclass(frozen=True, slots=True)
class OrcaProbeResult:
    available: bool
    checked_at: datetime
    cli_path: str | None = None
    version: str | None = None
    agents: tuple[str, ...] = ()
    worktree_root: str | None = None
    worktree_root_writable: bool = False
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "checked_at": self.checked_at.isoformat(),
            "cli_path": self.cli_path,
            "version": self.version,
            "agents": list(self.agents),
            "worktree_root": self.worktree_root,
            "worktree_root_writable": self.worktree_root_writable,
            "error": self.error,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OrcaProbeResult:
        return cls(
            available=bool(raw["available"]),
            checked_at=datetime.fromisoformat(raw["checked_at"]),
            cli_path=raw.get("cli_path"),
            version=raw.get("version"),
            agents=tuple(raw.get("agents") or ()),
            worktree_root=raw.get("worktree_root"),
            worktree_root_writable=bool(raw.get("worktree_root_writable")),
            error=raw.get("error"),
            details=dict(raw.get("details") or {}),
        )


# ---------------------------------------------------------------- 探测

async def probe(
    runner: Runner,
    *,
    cli_name: str = "orca",
    worktree_root: str | None = None,
    which: Any = None,
) -> OrcaProbeResult:
    """能力自检：CLI 路径 / 版本 / 可用 agent 种类 / worktree 根目录可写。

    四项里 CLI 缺失、版本不可解析、根目录不可写都判 available=False；
    agents 列举失败只降级（agents 为空），不算探测失败——spawn 由调用方
    在 available=False 时拒绝。
    """
    root = worktree_root if worktree_root is not None else default_worktree_root()
    checked_at = datetime.now(UTC)

    resolve = which or shutil.which  # 运行时解析，测试可替换
    cli_path = resolve(cli_name)
    if not cli_path:
        return OrcaProbeResult(
            available=False, checked_at=checked_at, worktree_root=root,
            error=f"未找到 Orca CLI：PATH 中无 `{cli_name}`，无法派 Orca 类任务",
        )

    try:
        version = _parse_version(
            await runner.run([cli_path, "--version"]), cli_name
        )
    except OrcaFormatError as exc:
        return OrcaProbeResult(
            available=False, checked_at=checked_at, cli_path=cli_path,
            worktree_root=root, error=str(exc),
        )

    agents, agents_error = await _probe_agents(runner, cli_name, cli_path)

    writable = os.path.isdir(root) and os.access(root, os.W_OK)
    if not writable:
        return OrcaProbeResult(
            available=False, checked_at=checked_at, cli_path=cli_path,
            version=version, agents=agents, worktree_root=root,
            worktree_root_writable=False,
            error=f"worktree 根目录不可写：{root}（不存在或无写权限）",
            details={"agents_error": agents_error} if agents_error else {},
        )

    return OrcaProbeResult(
        available=True, checked_at=checked_at, cli_path=cli_path,
        version=version, agents=agents, worktree_root=root,
        worktree_root_writable=True,
        details={"agents_error": agents_error} if agents_error else {},
    )


def _parse_version(result: RunnerResult, cli_name: str) -> str:
    """版本解析：命令失败/格式变化都抛 OrcaFormatError（明确错误，不空指针）。"""
    if result.rc != 0:
        raise OrcaFormatError(
            f"`{cli_name} --version` 退出码 {result.rc}：{result.stderr.strip()[:200]}"
        )
    match = _VERSION_RE.search(result.stdout)
    if not match:
        # 契约测试锚点：版本输出格式变化必须给出明确错误
        raise OrcaFormatError(
            "Orca CLI 输出格式变了：无法从 `orca --version` 输出解析版本号"
            f"（stdout={result.stdout.strip()[:120]!r}）"
        )
    return match.group(1)


async def _probe_agents(
    runner: Runner, cli_name: str, cli_path: str
) -> tuple[tuple[str, ...], str | None]:
    """agent 种类列举失败只降级，不影响 available。"""
    result = await runner.run([cli_path, "agents", "--json"])
    if result.rc != 0:
        return (), f"`{cli_name} agents --json` 不可用（rc={result.rc}）"
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return (), f"agents 输出不是合法 JSON：{result.stdout.strip()[:120]!r}"
    if isinstance(raw, dict):
        raw = raw.get("agents") or raw.get("kinds") or []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return tuple(names), None


# ---------------------------------------------------------------- 缓存与刷新

async def get_orca_status(*, force: bool = False) -> OrcaProbeResult:
    """读缓存 → 未命中则探测 → 写缓存（TTL 10 分钟）→ 启动周期刷新。

    Redis 不可用时探测照常返回（缓存只是加速器，不是可用性的前置条件）。
    """
    if not force:
        try:
            cached = await cache_get(PROBE_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orca 探测缓存读取失败", extra={"err": str(exc)})
            cached = None
        if cached:
            try:
                return OrcaProbeResult.from_dict(json.loads(cached))
            except (KeyError, ValueError, TypeError):
                logger.warning("orca 探测缓存损坏，重新探测")

    result = await probe(CliRunner())
    try:
        await cache_set(
            PROBE_CACHE_KEY, json.dumps(result.to_dict()), PROBE_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("orca 探测缓存写入失败", extra={"err": str(exc)})
    _ensure_refresher()
    return result


_refresher_task: asyncio.Task | None = None


def _ensure_refresher() -> None:
    """每 10 分钟后台重探测一次；幂等，只在有事件循环时启动。"""
    global _refresher_task
    if not REFRESHER_ENABLED:
        return
    if _refresher_task is not None and not _refresher_task.done():
        return
    try:
        _refresher_task = asyncio.create_task(_refresh_loop())
    except RuntimeError:  # 无运行中的事件循环（脚本/CLI 场景）→ 跳过
        logger.info("无事件循环，跳过 orca 探测刷新协程")


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(PROBE_TTL_SECONDS)
        try:
            result = await probe(CliRunner())
            await cache_set(
                PROBE_CACHE_KEY, json.dumps(result.to_dict()), PROBE_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("orca 周期探测失败", extra={"err": str(exc)})


# ---------------------------------------------------------------- 六原子动作（I04 起）

SPAWN_TIMEOUT_SECONDS = 15.0
SESSION_GONE_EXIT_CODE = 4  # 约定：会话不存在/已退出的退出码（adapter 吸收 CLI 方差）


class OrcaCommandError(OrcaError):
    """CLI 命令失败（非会话消亡的其他错误）。"""


class OrcaSessionGone(OrcaError):
    """会话不存在或已退出：调用方应把任务转 failed 并释放租约（重派）。"""


def _looks_like_session_gone(result: RunnerResult) -> bool:
    if result.rc == SESSION_GONE_EXIT_CODE:
        return True
    blob = f"{result.stdout}\n{result.stderr}"
    return "session_not_found" in blob


class OrcaAdapter:
    """spawn/send/steer/kill 的 CLI 适配（harvest 见 harvest.py；probe 见上文）。

    纪律：本类只做三件事——拼命令、解析 JSON、把失败翻译成语义化异常。
    会话=租约、事件流等语义一律在 service 层。CLI 子命令形态以本适配层
    为唯一绑定点，Orca 升级改这里，不改业务代码。
    """

    def __init__(self, runner: Runner, *, cli_name: str = "orca", which: Any = None) -> None:
        self._runner = runner
        self._cli_name = cli_name
        self._which = which

    def _resolve_cli(self) -> str:
        resolve = self._which or shutil.which  # 运行时解析，测试可替换
        path = resolve(self._cli_name)
        if not path:
            raise OrcaError(f"未找到 Orca CLI：PATH 中无 `{self._cli_name}`")
        return path

    async def spawn(
        self,
        *,
        worktree: str,
        task_id: str,
        prompt: str | None = None,
        snapshot: str | None = None,
    ) -> str:
        """启动会话：返回 Orca session_id。建目录失败 → OrcaCommandError（可重试）。"""
        cli = self._resolve_cli()
        args = [cli, "spawn", "--json", "--worktree", worktree, "--task", task_id]
        if prompt:
            args += ["--prompt", prompt]
        if snapshot:
            args += ["--snapshot", snapshot]
        result = await self._runner.run(args, timeout=SPAWN_TIMEOUT_SECONDS)
        if result.rc != 0:
            raise OrcaCommandError(
                f"`{self._cli_name} spawn` 失败（rc={result.rc}）：{result.stderr.strip()[:200]}"
            )
        try:
            session_id = json.loads(result.stdout)["session_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # 契约锚点：spawn 输出格式变化必须给出明确错误
            raise OrcaFormatError(
                "Orca CLI 输出格式变了：spawn 结果缺 session_id"
                f"（stdout={result.stdout.strip()[:120]!r}）"
            ) from exc
        return str(session_id)

    async def _control(self, args: list[str], *, action: str) -> None:
        """send/steer/kill 共用：rc=0 受理；会话消亡与一般失败分开翻译。"""
        result = await self._runner.run(args)
        if result.rc == 0:
            return
        if _looks_like_session_gone(result):
            raise OrcaSessionGone(
                f"`{self._cli_name} {action}` 发现会话已不存在或已退出"
                f"（rc={result.rc}）：{result.stderr.strip()[:160]}"
            )
        raise OrcaCommandError(
            f"`{self._cli_name} {action}` 失败（rc={result.rc}）：{result.stderr.strip()[:200]}"
        )

    async def send(self, *, session: str, prompt: str, files: list[str] | None = None) -> None:
        """向会话注入 prompt（附上下文文件）。会话不存在 → OrcaSessionGone（重派）。"""
        cli = self._resolve_cli()
        args = [cli, "send", "--json", "--session", session, "--prompt", prompt]
        for path in files or []:
            args += ["--file", path]
        await self._control(args, action="send")

    async def steer(self, *, session: str, message: str) -> None:
        """人的干预指令（闸门/纠偏）：只做生命周期级干预，不指挥它怎么想。"""
        cli = self._resolve_cli()
        args = [cli, "steer", "--json", "--session", session, "--message", message]
        await self._control(args, action="steer")

    async def kill(self, *, session: str, mode: str = "graceful") -> None:
        """终止会话。graceful = 先 snapshot 再杀（断点续跑依据）；hard = 直接杀。"""
        cli = self._resolve_cli()
        args = [cli, "kill", "--json", "--session", session, "--mode", mode]
        await self._control(args, action="kill")
