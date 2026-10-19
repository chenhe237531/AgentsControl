"""w03_03: merge heads（w03 链 × w07 链）

Revision ID: w03_03_merge_heads
Revises: w03_02_dead_letter, w07_02_approvals
Create Date: 2026-10-19

窗口: W03

W03 任务队列链（w03_01_tasks → w03_02_dead_letter，基于 w01_03_merge_heads）与
main 上已合入的 W07 账本链（w02_01_intake → w07_01_ledger → w07_02_approvals）
并行分叉，本 revision 把两条链合并回单一 head。
无 schema 变更（alembic merge 节点）。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "w03_03_merge_heads"
# 注解保持 str | None 与项目静态检查约定一致；值可以是 tuple（alembic 原生支持）
down_revision: str | None = ("w03_02_dead_letter", "w07_02_approvals")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
