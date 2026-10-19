"""evals 模块 ORM 模型。

三张表对应评测的三个阶段：
- `eval_tasks`   题库（20 道自有题，8 个维度）；
- `eval_runs`    一次评测运行（异步任务，含成本上限与终态）；
- `eval_results` 单题 × 单模型的结果（score / cost / latency / raw_ref）。

设计要点：
- 分数用 `Numeric(5, 2)`，成本用 `Numeric(12, 6)`；**不用 Float**，
  避免"两次 0.1 相加等于 0.30000000000000004"这类在成本对账里致命的误差。
- `eval_results.cache_key` 是 `(model, task, params)` 的稳定哈希，7 天缓存靠它命中；
  缓存命中仍然写一行 `cached=True` 的结果，保证"某次运行看到了什么"可复现。
- 题目不做物理删除：`DELETE /evals/tasks/{id}` 是软删（`active=False` + `archived_at`），
  否则历史 run 的结果会指向不存在的题。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EvalTask(Base):
    """一道评测题。dimension 必须在 8 个维度之内（校验在 service 层）。"""

    __tablename__ = "eval_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # [{"criterion": "...", "weight": 0.5}, ...]；空表示由 provider 自行打分
    rubric: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    max_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=100)
    weight: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class EvalRun(Base):
    """一次评测运行。`POST /evals/runs` 只创建它，执行发生在请求之外。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="fake")
    models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    task_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 单次运行成本上限（人民币元）。超限中止，不允许"评测成为账外支出"。
    cost_cap_cny: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=20)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EvalResult(Base):
    """单题 × 单模型的结果。验收要求四个字段齐全：model / cost_usd / latency_ms / score。"""

    __tablename__ = "eval_results"
    __table_args__ = (
        UniqueConstraint("run_id", "task_id", "model", name="uq_eval_results_run_task_model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 原始输出/日志的引用（对象存储 key 或行内摘要），便于事后复核打分
    raw_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


__all__ = ["EvalTask", "EvalRun", "EvalResult"]
