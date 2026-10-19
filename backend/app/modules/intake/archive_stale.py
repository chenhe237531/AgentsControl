"""14 天未结构化的 raw intake 条目归档为「兴趣池」。

约束（plan/W02.md §2 与 W02-I05 验收）：
- 仅更新 status='raw' 的条目，重复运行结果一致（幂等）；
- 不发送任何通知（催办会让人不再记录）；
- 由外部 cron 调用，不放进请求处理器；
- 提供 --dry-run 仅列出候选而不更新；
- 14 天阈值通过配置可调，**不硬编码**在脚本里。

调用方式：
    python -m app.modules.intake.archive_stale --dry-run
    python -m app.modules.intake.archive_stale --days 14 --limit 500

注：路径放在 `app/modules/intake/archive_stale.py` 而非 plan/W02.md 写的
`scripts/archive_stale_intake.py`，是因为 plan/windows.yaml 的 W02 allow
不包含仓库根 scripts/（属 W01）。该路径冲突已在 .fleet/handoff/W02.md 记下，
由 Marshal 在波次边界决定是否走 shared-registry 把 scripts/ 加入 W02 allow。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy import and_, select, update

from app.core.config import settings
from app.core.db import dispose_engine, get_sessionmaker

from .models import IntakeItem

logger = logging.getLogger("afc.intake.archive_stale")

# 默认阈值：14 天未结构化。可在 settings 加 INTAKE_STALE_DAYS 覆盖。
DEFAULT_STALE_DAYS = 14
DEFAULT_LIMIT = 500


async def list_stale(before: datetime, *, limit: int) -> list[IntakeItem]:
    sm = get_sessionmaker()
    async with sm() as session:
        stmt = (
            select(IntakeItem)
            .where(
                and_(
                    IntakeItem.status == "raw",
                    IntakeItem.created_at < before,
                )
            )
            .order_by(IntakeItem.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars())


async def archive(item_ids: list[str], *, at: datetime) -> int:
    if not item_ids:
        return 0
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            stmt = (
                update(IntakeItem)
                .where(
                    and_(
                        IntakeItem.id.in_(item_ids),
                        IntakeItem.status == "raw",
                    )
                )
                .values(status="archived_interest", archived_at=at)
            )
            result = await session.execute(stmt)
            return int(result.rowcount or 0)


async def run(*, dry_run: bool, days: int, limit: int) -> int:
    """主入口。返回归档条数（dry_run 时返回候选数）。"""
    before = datetime.utcnow() - timedelta(days=days)
    candidates = await list_stale(before, limit=limit)
    logger.info(
        "intake archive scan: candidates=%d before=%s days=%d dry_run=%s",
        len(candidates),
        before.isoformat(),
        days,
        dry_run,
    )

    if not candidates:
        return 0

    if dry_run:
        for c in candidates[:20]:
            logger.info(
                "  candidate id=%s created=%s text=%s",
                c.id,
                c.created_at.isoformat(),
                c.text[:60],
            )
        if len(candidates) > 20:
            logger.info("  ... and %d more (truncated)", len(candidates) - 20)
        return len(candidates)

    ids = [c.id for c in candidates]
    n = await archive(ids, at=datetime.utcnow())
    logger.info("archived %d items (out of %d candidates)", n, len(candidates))
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 14 天未结构化的 intake 条目归档为兴趣池。"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="仅列出候选而不更新数据库"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"未结构化的阈值天数（默认 {DEFAULT_STALE_DAYS}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"单次最多处理条数（默认 {DEFAULT_LIMIT}）",
    )
    args = parser.parse_args(argv)

    # 启用结构化日志（脚本独立运行时）
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        n = asyncio.run(run(dry_run=args.dry_run, days=args.days, limit=args.limit))
        print(f"{'candidates' if args.dry_run else 'archived'}: {n}")
        return 0
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    sys.exit(main())