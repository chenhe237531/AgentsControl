"""w06_03: merge heads（w06 链 × main）

Revision ID: w06_03_merge_heads
Revises: w06_02_sessions, w03_03_merge_heads
Create Date: 2026-10-19

窗口: W06

W06 执行面链（w06_01_workers → w06_02_sessions，基于 0001_baseline）与
main 上已合入的 W03/W07 合流链（w03_03_merge_heads）并行分叉，
本 revision 把两条链合并回单一 head。
无 schema 变更（alembic merge 节点）。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "w06_03_merge_heads"
# 注解保持 str | None 与项目静态检查约定一致；值可以是 tuple（alembic 原生支持）
down_revision: str | None = ("w06_02_sessions", "w03_03_merge_heads")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
