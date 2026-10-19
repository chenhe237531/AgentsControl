#!/usr/bin/env python3
"""回收过期租约（后台任务入口）。

用法::

    python scripts/reclaim_leases.py                 # 回收 + 把到期的 failed 放回队列
    python scripts/reclaim_leases.py --dry-run       # 只打印将被回收的任务 id
    python scripts/reclaim_leases.py --no-retry      # 只回收，不动重试队列
    python scripts/reclaim_leases.py --limit 50 --json

为什么要有这个脚本：
租约过期是**必然事件**（设备断电、进程被杀、网络分区）。没有回收，任务会永远停在
`leased`，队列看起来"没活可派"但其实是"活被卡住了"。这个脚本按 cron/定时任务跑，
每次只做一条 SQL 的批量更新，幂等，重复执行无副作用。

退出码：0 正常 / 1 数据库或配置错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.db import dispose_engine, get_sessionmaker
from app.modules.tasks import service
from app.modules.tasks.settings import task_settings


async def _run(args: argparse.Namespace) -> int:
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            result = await service.reclaim_expired(
                session, limit=args.limit, dry_run=args.dry_run
            )
            retried: list[str] = []
            if not args.dry_run and not args.no_retry:
                ids = await service.promote_due_retries(session, limit=args.limit)
                retried = [str(i) for i in ids]
    finally:
        await dispose_engine()

    payload = {
        "dry_run": args.dry_run,
        "lease_seconds": task_settings.lease_seconds,
        "heartbeat_seconds": task_settings.heartbeat_seconds,
        "reclaim_max": task_settings.reclaim_max,
        "reclaimed": result["reclaimed"],
        "dead": result["dead"],
        "requeued_retries": len(retried),
        "task_ids": result["ids"],
        "dead_ids": result["dead_ids"],
        "retry_ids": retried,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        prefix = "[dry-run] 将回收" if args.dry_run else "已回收"
        print(f"{prefix} {result['reclaimed']} 个过期租约（进死信 {result['dead']} 个）")
        for task_id in result["ids"]:
            print(f"  - {task_id}")
        if retried:
            print(f"重投 {len(retried)} 个到达退避时间的失败任务")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回收过期任务租约并推进重试队列")
    parser.add_argument("--dry-run", action="store_true", help="只列出将被回收的任务 id，不写库")
    parser.add_argument("--limit", type=int, default=None, help="单次处理上限（默认取配置）")
    parser.add_argument("--no-retry", action="store_true", help="不把到期的 failed 任务放回队列")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - CLI 需要把原因打给人看
        print(f"回收失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
