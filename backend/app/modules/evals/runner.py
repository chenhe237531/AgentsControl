"""评测运行器：把"一次 run"跑完，不在 HTTP 请求里等模型。

三条硬规则（对应 plan/W08.md 的验收与成本控制）：
1. **成本上限**：累计成本换算成人民币后超过 `run.cost_cap_cny` 立即中止（status=aborted，
   error_code=cost_cap_exceeded）。已经花掉的那一条仍然入库——否则账对不上。
2. **7 天缓存**：同 model + 同题 + 同题目版本（`task_params_sig`）命中既有结果时
   不再调用 provider，写一行 `cached=True` 的结果，`cost_usd=0`。
3. **失败要留痕**：provider 抛异常 → status=failed + error_code=provider_error，
   已产出的结果正常入库，不做"全丢"处理。

provider 是 Protocol：真模型走 LiteLLM（后续 issue 接），测试用 FakeProvider，不出网。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.core.errors import ValidationError

from .models import EvalResult, EvalRun, EvalTask
from .repository import EvalRepository, cache_key, new_id, task_params_sig

logger = logging.getLogger("afc.evals.runner")

CACHE_TTL = timedelta(days=7)
DEFAULT_CAP_CNY = 20.0
SUPPORTED_PROVIDERS = ("fake",)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """一次打分的原始产出。score 已归一到 [0, task.max_score]。"""

    score: float
    cost_usd: float
    latency_ms: int
    raw_ref: str


class EvalProvider(Protocol):
    async def score(self, *, model: str, task: EvalTask) -> ProviderResult: ...


class FakeProvider:
    """确定性假 provider：不联网、不花钱、可复现。

    分数由 (model, task.id) 稳定哈希落在 55–94 之间——刻意留出"不及格"区间，
    这样能力上限标记（I03）在本地就能被测到，而不是永远一片绿。
    """

    def __init__(self, *, cost_per_call_usd: float = 0.002, latency_ms: int = 120) -> None:
        self.cost_per_call_usd = cost_per_call_usd
        self.latency_ms = latency_ms
        self.calls = 0

    async def score(self, *, model: str, task: EvalTask) -> ProviderResult:
        self.calls += 1
        digest = cache_key(model, task.id, task_params_sig(task))
        bucket = int(digest[:4], 16) % 40
        score = float(min(float(task.max_score), 55 + bucket))
        return ProviderResult(
            score=round(score, 2),
            cost_usd=self.cost_per_call_usd,
            latency_ms=self.latency_ms,
            raw_ref=f"fake://{model}/{task.id}/{digest[:12]}",
        )


def provider_for(name: str) -> EvalProvider:
    if name not in SUPPORTED_PROVIDERS:
        raise ValidationError(
            f"暂不支持的 provider: {name}（当前只支持 {', '.join(SUPPORTED_PROVIDERS)}）"
        )
    return FakeProvider()


def usd_cny_rate() -> float:
    """汇率取环境变量，默认 7.2。放在这里而不是 core/config：core/ 是冻结文件。"""
    try:
        return float(os.environ.get("EVAL_USD_CNY_RATE", "7.2"))
    except ValueError:
        return 7.2


def default_cost_cap_cny() -> float:
    try:
        return float(os.environ.get("EVAL_MAX_COST_CNY", str(DEFAULT_CAP_CNY)))
    except ValueError:
        return DEFAULT_CAP_CNY


async def execute_run(
    repo: EvalRepository,
    run: EvalRun,
    tasks: list[EvalTask],
    provider: EvalProvider,
    *,
    now: datetime | None = None,
) -> EvalRun:
    """同步跑完一次 run（由后台任务调用）。返回终态 run。"""
    started = now or datetime.utcnow()
    run = await repo.update_run(run.id, {"status": "running", "started_at": started})

    results: list[EvalResult] = []
    spent_usd = 0.0
    cache_hits = 0
    rate = usd_cny_rate()
    terminal_error: tuple[str, str] | None = None

    for model in list(run.models):
        for task in tasks:
            key = cache_key(model, task.id, task_params_sig(task))
            cached = await repo.find_cached(key, since=started - CACHE_TTL)
            if cached is not None:
                cache_hits += 1
                results.append(
                    EvalResult(
                        id=new_id(),
                        run_id=run.id,
                        task_id=task.id,
                        model=model,
                        score=cached.score,
                        cost_usd=0,
                        latency_ms=cached.latency_ms,
                        raw_ref=cached.raw_ref,
                        cached=True,
                        cache_key=key,
                        created_at=started,
                    )
                )
                continue

            try:
                outcome = await provider.score(model=model, task=task)
            except Exception as exc:  # noqa: BLE001 - provider 异常必须转成 run 终态而不是 500
                logger.warning("eval provider failed", extra={"run_id": run.id, "model": model})
                terminal_error = ("provider_error", f"{type(exc).__name__}: {exc}"[:500])
                break

            results.append(
                EvalResult(
                    id=new_id(),
                    run_id=run.id,
                    task_id=task.id,
                    model=model,
                    score=outcome.score,
                    cost_usd=outcome.cost_usd,
                    latency_ms=outcome.latency_ms,
                    raw_ref=outcome.raw_ref,
                    cached=False,
                    cache_key=key,
                    created_at=started,
                )
            )
            spent_usd += outcome.cost_usd

            if spent_usd * rate > float(run.cost_cap_cny):
                terminal_error = (
                    "cost_cap_exceeded",
                    f"成本上限 {float(run.cost_cap_cny):.2f} 元已超（已花 {spent_usd * rate:.2f} 元），运行中止",
                )
                break
        if terminal_error is not None:
            break

    await repo.add_results(results)

    if terminal_error is not None:
        code, message = terminal_error
        status = "failed" if code == "provider_error" else "aborted"
        return await repo.update_run(
            run.id,
            {
                "status": status,
                "cost_usd": round(spent_usd, 6),
                "result_count": len(results),
                "cache_hits": cache_hits,
                "error_code": code,
                "error_message": message,
                "finished_at": datetime.utcnow(),
            },
        )

    return await repo.update_run(
        run.id,
        {
            "status": "succeeded",
            "cost_usd": round(spent_usd, 6),
            "result_count": len(results),
            "cache_hits": cache_hits,
            "finished_at": datetime.utcnow(),
        },
    )


__all__ = [
    "CACHE_TTL",
    "DEFAULT_CAP_CNY",
    "EvalProvider",
    "FakeProvider",
    "ProviderResult",
    "default_cost_cap_cny",
    "execute_run",
    "provider_for",
    "usd_cny_rate",
]
