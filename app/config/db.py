"""SQLAlchemy 异步引擎与会话工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
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


@event.listens_for(engine.sync_engine, "connect")
def _pin_session_timezone(dbapi_connection, connection_record) -> None:
    """把每个新连接的**会话时区**钉到 UTC。

    为什么必须钉：模型里的时间列用的是 ``server_default=sa.func.now()``，
    也就是让数据库自己填当前时间。两种方言对此的处理不一样：

    - SQLite 的 ``CURRENT_TIMESTAMP`` 返回 **UTC**，本来就对；
    - MySQL 的 ``NOW()`` 返回**会话时区**的时间，而会话时区默认继承**服务器
      系统时区**。一台设成 Asia/Shanghai 的 MySQL 就会把 +08:00 的本地时间
      写进库里。

    于是在中国本机用 MySQL 跑，时间戳会整整差 8 小时；多节点时区不一致时，
    同一张表里的时间线会直接错乱。钉死成 ``+00:00`` 之后，MySQL 与 SQLite
    写入的值语义一致，都是 UTC。

    ``+00:00`` 用的是**固定偏移**而不是 ``UTC``/``SYSTEM`` 这类时区名：后者
    要求 MySQL 装了时区表（``mysql_tzinfo_to_sql``），很多精简版镜像没装，
    会在建连接时报错。固定偏移无依赖。

    **只对 MySQL 生效**：判据取 ``engine.dialect.name`` 而不是 dbapi 连接的类名
    ——异步 SQLite 的驱动包叫 ``aiosqlite``，按类名判断会漏掉它，然后对着
    SQLite 发一条 ``SET time_zone`` 直接语法错误（``near "SET": syntax error``）。

    已有数据不会被这一步修正——历史上按服务器本地时间写入的行仍需一次性
    ``CONVERT_TZ``，见设计文档 §15.3。
    """
    if engine.dialect.name != "mysql":
        return
    with dbapi_connection.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")


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
