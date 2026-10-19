"""SQLAlchemy 异步引擎与会话工厂。"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


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


# WAL 只在**第一条连接**上切一次：``PRAGMA journal_mode=WAL`` 需要库的排他锁，
# 而 SQLite 是 NullPool（每个会话一条新连接），每条连接都执行一次的话，
# 撞上别的连接持有的读事务就会一直等——实测会把测试套件偶发挂住。
# 切过一次之后模式是**持久**的（记在库文件头里），后续连接不用再切。
_sqlite_wal_ready = False


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record) -> None:
    """给 SQLite 连接配上并发下能用的参数。

    ## 三个问题，三条对策

    SQLite 的默认配置在**并发写**下必然出错，而且错得很直接。

    **问题一：默认日志模式是 ``delete``（回滚日志）。**
    写事务期间对整个库加**排他锁**，读者也被挡住。

    对策：``journal_mode=WAL``。写者与读者不再互斥。这是最关键的一条，
    **每进程只切一次**（见上面的说明）——切它要拿库的排他锁，每条连接都切会
    撞上别的连接持有的读事务，把测试套件偶发挂住。

    **问题二：默认 ``busy_timeout`` 只有 5 秒。**
    撞上别的写者时，SQLite 最多等 5 秒就抛 ``database is locked``。

    对策：``busy_timeout=15000``，撞上时排队等而不是立刻失败。连接级参数。

    **问题三：SQLite 只有一个写者，并发写必然排队。**
    这条**没有配置层面的解**——见下面的「已知上限」。

    ## 已知上限：SQLite 的并发天花板

    实测（本机 4 核，见 ``docs/performance.md``）：并发 **32** 个请求同时建 run
    没问题；**64** 个开始出现 ``database is locked``。

    还有一个更隐蔽的坑：WAL 模式下，一个「先读后写」的事务（deferred BEGIN）
    先拿到读快照，之后要写时若别的连接已提交过，SQLite **立刻**返回
    ``database is locked``，而且**不走 busy handler**——等多久都没用。
    本项目正好踩中：``POST /api/conversations/{id}/messages`` 在同一个事务里
    先「锁会话行 + 查进行中的 run」（读），再「建 run + 落用户消息」（写）。

    试过用 ``BEGIN IMMEDIATE`` 解决（SQLAlchemy 官方并发写配方），**放弃**：
    它让**每个**事务一上来就握写锁，于是任何「跨 await 持有事务」的地方都会
    把别人堵死——本项目的 SSE 流整个生命周期都占着一个请求级事务，一开流
    别的写入就全挂。**把报错变成死锁更糟**，所以保持 deferred，只靠 WAL +
    busy_timeout 缓解。

    结论：**SQLite 只适合开发与单机小流量**。生产用 MySQL（行级锁 + MVCC），
    本节这套 pragma 对 MySQL 完全不生效（见 ``_engine_kwargs``）。

    ## 边界

    - **只对 SQLite 生效**。
    - **WAL 要求文件在本地文件系统上**：网络盘不支持。设置失败时静默降级回默认
      模式（并在日志里留一条），不能因此让服务起不来。
    """
    global _sqlite_wal_ready
    if engine.dialect.name != "sqlite":
        return
    cur = dbapi_connection.cursor()
    try:
        # 连接级参数：不涉及任何锁，可以每条连接都设
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA synchronous=NORMAL")
        if not _sqlite_wal_ready:
            # 先读当前模式：已经是 WAL 就不用再切（切它要拿排他锁）
            cur.execute("PRAGMA journal_mode")
            row = cur.fetchone()
            if not row or str(row[0]).lower() != "wal":
                cur.execute("PRAGMA journal_mode=WAL")
            _sqlite_wal_ready = True
    except Exception:  # noqa: BLE001 - pragma 失败不该让服务起不来
        logger.warning("SQLite pragma 配置失败，回退默认模式", exc_info=True)
    finally:
        cur.close()


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
