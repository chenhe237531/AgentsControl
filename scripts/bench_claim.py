#!/usr/bin/env python3
"""领取延迟压测：输出 P50 / P95 与吞吐（W03-I05 验收 ③）。

用法::

    python scripts/bench_claim.py --tasks 500 --workers 8
    python scripts/bench_claim.py --tasks 200 --workers 4 --json

说明：
- 直接在真实 Postgres 上跑（`SKIP LOCKED` 的行为只有真库能验证，mock 没有意义）；
- 任务全部是「无能力要求、无预算上限」的最小行，测的是队列本身，不含业务校验；
- 压测数据用 `title` 前缀标记，结束后自动清理，不会污染真实队列。
- 结果写进 PR 描述（W03-I05 验收要求）。

退出码：0 正常 / 1 环境不就绪。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa
from app.core.db import dispose_engine, get_sessionmaker
from app.modules.tasks import repository
from app.modules.tasks.settings import task_settings

BENCH_PREFIX = "__bench_claim__"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


async def _cleanup(session) -> None:
    await session.execute(
        sa.text("DELETE FROM tasks WHERE title LIKE :prefix"), {"prefix": f"{BENCH_PREFIX}%"}
    )
    await session.commit()


async def _seed(session, count: int) -> None:
    for i in range(count):
        await repository.create_task(
            session,
            title=f"{BENCH_PREFIX}{i}",
            goal_id=None,
            payload={},
            requires={},
            # 均匀铺开 0–9，避免全部同优先级导致测的是"顺序扫描"
            priority=i % 10,
            state="queued",
            budget_cap=None,
            max_retries=3,
            retryable=True,
        )
    await session.commit()


async def _worker(device_id: str, latencies: list[float], stop: asyncio.Event) -> int:
    claimed = 0
    sessionmaker = get_sessionmaker()
    while not stop.is_set():
        async with sessionmaker() as session:
            started = time.perf_counter()
            task = await repository.claim_next(
                session,
                device_id=device_id,
                caps={},
                lease_seconds=task_settings.lease_seconds,
                force_low=False,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if task is None:
                await session.commit()
                stop.set()
                break
            await repository.touch_device_stats(session, device_id, consecutive_high=0)
            await session.commit()
            latencies.append(elapsed_ms)
            claimed += 1
    return claimed


async def _run(args: argparse.Namespace) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        exists = await session.scalar(sa.text("SELECT to_regclass('public.tasks') IS NOT NULL"))
        if not exists:
            print("tasks 表不存在，先跑 alembic upgrade head", file=sys.stderr)
            return 1
        await _cleanup(session)
        foreign = await session.scalar(
            sa.text(
                "SELECT count(*) FROM tasks WHERE state = 'queued' AND title NOT LIKE :prefix"
            ),
            {"prefix": f"{BENCH_PREFIX}%"},
        )
        if foreign and not args.force:
            print(
                f"队列里还有 {foreign} 个非压测任务。压测会连它们一起领走，"
                "请在独立库上跑，或加 --force 明确接受干扰。",
                file=sys.stderr,
            )
            return 1
        await _seed(session, args.tasks)

    latencies: list[float] = []
    stop = asyncio.Event()
    started = time.perf_counter()
    claimed = await asyncio.gather(
        *(_worker(f"bench-{i}-{uuid.uuid4().hex[:6]}", latencies, stop) for i in range(args.workers))
    )
    wall = time.perf_counter() - started

    async with sessionmaker() as session:
        await _cleanup(session)
    await dispose_engine()

    total = sum(claimed)
    report = {
        "tasks": args.tasks,
        "workers": args.workers,
        "claimed": total,
        "wall_seconds": round(wall, 3),
        "throughput_per_sec": round(total / wall, 1) if wall else 0.0,
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "max_ms": round(max(latencies), 2) if latencies else 0.0,
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else 0.0,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"任务 {report['tasks']} / 并发 worker {report['workers']} / 实际领到 {total}")
        print(f"P50 {report['p50_ms']} ms　P95 {report['p95_ms']} ms　max {report['max_ms']} ms")
        print(f"吞吐 {report['throughput_per_sec']}/s（墙钟 {report['wall_seconds']} s）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="任务领取延迟压测（需要真实 Postgres）")
    parser.add_argument("--tasks", type=int, default=500)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force", action="store_true", help="队列里有真实任务时仍继续压测")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"压测失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
