"""admin-web 独立数据库会话。"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings


def _admin_url() -> str:
    """返回管理员服务数据库 URL，未单独配置时复用主库地址。"""
    return settings.admin_database_url.strip() or settings.database_url


def _engine_kwargs(url: str) -> dict:
    """构造与主服务一致、但受后台连接池上限保护的引擎参数。"""
    if url.startswith("sqlite"):
        return {"poolclass": NullPool}
    kwargs: dict = {
        "pool_size": max(2, min(settings.db_pool_size, 10)),
        "max_overflow": max(0, min(settings.db_max_overflow, 10)),
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle,
    }
    if url.startswith("postgresql"):
        connect_args: dict[str, object] = {"prepare_threshold": None}
        if settings.db_pg_timezone and "options" not in make_url(url).query:
            connect_args["options"] = (
                f"-c timezone={settings.db_pg_timezone}"
            )
        kwargs["connect_args"] = connect_args
    return kwargs


_url = _admin_url()
admin_engine = create_async_engine(
    _url,
    echo=settings.db_echo,
    future=True,
    **_engine_kwargs(_url),
)


@event.listens_for(admin_engine.sync_engine, "connect")
def _pin_mysql_timezone(dbapi_connection, _connection_record) -> None:
    """admin-web 也把 MySQL 会话时区固定为 UTC。"""
    if admin_engine.dialect.name != "mysql":
        return
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")


AdminSessionLocal = async_sessionmaker(
    bind=admin_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def dispose_admin_engine() -> None:
    """关闭 admin-web 专用连接池。"""
    await admin_engine.dispose()
