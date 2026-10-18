"""数据库访问。

- 全异步（asyncpg），连接池参数来自配置；
- engine 延迟创建：导入本模块不会连库，便于测试与「无库启动」；
- `/ready` 走 `ping_database()`，超时即视为未就绪（不拖垮 liveness）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger("afc.db")

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。各模块在自己的 `models.py` 中继承它。"""


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个会话。业务层不 commit，由服务层显式提交事务。"""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """多步写入必须使用事务；失败整体回滚。"""
    async with get_sessionmaker()() as session:
        async with session.begin():
            yield session


async def ping_database(timeout: float = 2.0) -> tuple[bool, str]:
    try:
        async with asyncio.timeout(timeout):
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - 健康检查需要吞掉所有异常并降级
        logger.warning("database ping failed", extra={"err": str(exc)})
        return False, type(exc).__name__


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
