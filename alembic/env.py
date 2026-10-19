"""Alembic 环境：异步引擎 + 自动取 URL。

迁移目标库与运行时用同一个 ``DATABASE_URL``，避免「迁移到 A 库、服务连 B 库」。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.config.settings import settings
from app.models import Base  # noqa: F401  确保所有表都注册到 metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    """在线模式的 alembic 配置：连类型一起比 + SQLite 用 batch 改表。

    ``compare_type`` 防「只改了列名没改类型」漏检；``render_as_batch`` 让
    SQLite 的 ALTER（加列/改约束）走临时表重建，否则生成不出可执行迁移。

    ``transaction_per_migration=True`` 是**每个迁移各自开一个事务**，不是
    「关闭事务包裹」——后者是 ``transactional_ddl=False``。这里要前者的原因：
    MySQL 的 DDL 会隐式提交，把多条 ALTER 塞进同一个事务既做不到原子，
    中途失败还会留下「改了一半」的表。按迁移独立提交，失败粒度反而更清晰。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    """离线模式（``alembic upgrade head --sql``）：只生成 SQL，不连库。

    ``literal_binds=True`` 把参数直接写进 SQL 文本，否则脚本里还是 ``?``，
    贴到别的客户端执行不了。
    """
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """同步侧的迁移入口，供 ``run_sync`` 从 async 连接调用。"""
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：用异步引擎连库，把同步的 alembic 上下文 ``run_sync`` 过去。

    ``poolclass=pool.NullPool`` + 末尾 ``dispose()``：迁移是一次性动作，
    不需要连接池，留着只会让子进程结束时挂住（SQLite 文件锁）。
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
