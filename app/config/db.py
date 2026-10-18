"""SQLAlchemy 异步引擎与会话工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config.settings import settings


def _engine_kwargs() -> dict:
    """不同方言的连接池参数不同：SQLite 不接受 pool_size / pool_recycle。

    SQLite 一律用 NullPool——异步场景下 SQLite 连接跨事件循环复用会导致
    "attached to a different loop"，且 SQLite 本身也不需要连接池。
    """
    url = settings.database_url
    if url.startswith("sqlite"):
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle,
    }


engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    future=True,
    **_engine_kwargs(),
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 避免异步上下文中意外触发懒加载
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级 AsyncSession。"""
    async with SessionLocal() as session:
        yield session


async def dispose_engine() -> None:
    """关闭引擎并归还连接池；进程退出/测试重置时调用，避免连接泄漏。"""
    await engine.dispose()
