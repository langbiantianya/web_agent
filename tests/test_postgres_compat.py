"""PostgreSQL 适配的可移植性测试（V3.3）。

「换个 URL 就能跑」在数据库这件事上是不成立的——有三处必须真的对上：

1. **DDL 能不能在 PG 上建出来**。用 PG 方言把整张 schema 渲染一遍，断言
   没有 MySQL 专属的语法漏进来（``AUTO_INCREMENT``、反引号、``DATETIME(``），
   并且 PG 该用的类型真的出现了（``BIGSERIAL``、``JSON``、
   ``TIMESTAMP WITHOUT TIME ZONE``）。
2. **迁移脚本能不能在 PG 上跑**。用 ``alembic upgrade head --sql`` 离线生成
   PG 的 DDL：不需要 PG 服务器，却能把三个迁移脚本连同方言分支一起验一遍。
3. **会话时区钉不钉得住**。PG 的 ``now()`` 返回带时区的时间，落到无时区的
   ``TIMESTAMP`` 列时按**会话时区**折算；不钉成 UTC，中国本机写出来的
   时间戳会整整差 8 小时。

这些断言全部**不需要 PG 服务器**：方言编译与离线 SQL 生成都是纯本地计算。
真连库的行为（约束是否生效、JSON 是否能往返）留给部署环境的集成测试。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.config import db as db_mod
from app.models import Base

ROOT = Path(__file__).resolve().parents[1]

_PG_URL = "postgresql+psycopg://user:pass@127.0.0.1:5432/agentdb"
_MYSQL_URL = "mysql+asyncmy://user:pass@127.0.0.1:3306/agentdb?charset=utf8mb4"

# MySQL 专属、PG 上必然报错的写法。渲染出来的 DDL 里出现任何一个就是坑。
_MYSQL_ONLY_MARKERS = (
    "AUTO_INCREMENT",
    "`",  # MySQL 的反引号标识符
    "DATETIME(",
    "ENGINE=",
    "UNSIGNED",
)


def _ddl_for(dialect) -> str:
    """把全部表按给定方言渲染成 DDL 文本。

    Args:
        dialect: SQLAlchemy 方言实例。

    Returns:
        拼接后的 DDL 文本。
    """
    return "\n".join(
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    )


def test_schema_ddl_is_valid_postgresql():
    """PG 方言渲染出的 DDL 必须是 PG 认得的，且不夹带 MySQL 语法。"""
    ddl = _ddl_for(postgresql.dialect())

    for marker in _MYSQL_ONLY_MARKERS:
        assert marker not in ddl, f"PG 的 DDL 里混进了 MySQL 专属写法：{marker}"

    # PG 该有的样子
    assert "BIGSERIAL" in ddl, "自增主键没有渲染成 BIGSERIAL，插入时拿不到 id"
    assert "JSON" in ddl, "JSON 列没有渲染成 PG 原生 JSON"
    assert "TIMESTAMP WITHOUT TIME ZONE" in ddl, "时间列不是无时区的 TIMESTAMP"
    assert "VARCHAR(64)" in ddl and "TEXT" in ddl


def test_schema_ddl_is_still_valid_for_mysql_and_sqlite():
    """加 PG 不能把已有的两个方言弄坏——这是回归护栏。"""
    my = _ddl_for(mysql.dialect())
    assert "AUTO_INCREMENT" in my, "MySQL 的自增主键不见了"
    assert "JSON" in my

    lite = _ddl_for(sqlite.dialect())
    # SQLite 的自增靠「INTEGER PRIMARY KEY」这个 **rowid 别名**，不需要
    # AUTOINCREMENT 关键字（那只是防止 id 被复用）。所以断言的是列类型
    # 恰好是 INTEGER——写成 BIGINT 就不 alias 了，插入时拿不到 id。
    assert "id INTEGER NOT NULL" in lite, "SQLite 的自增主键不再是 rowid 别名了"
    assert "BIGSERIAL" not in lite, "SQLite 上不该出现 BIGSERIAL"
    assert "JSON" in lite, "SQLite 的 JSON 列不见了"


def test_three_dialects_share_the_same_tables_and_columns():
    """三种方言的**表与列**必须完全一致——差异只允许出现在类型渲染上。

    这是「同一套模型跑三种库」的核心保证：少一列、改个名，跨库的部署就会
    在某个方言上炸掉，而这种差异在单库测试里永远看不见。
    """
    def _shape(dialect) -> dict[str, list[str]]:
        """取某个方言下每张表的列名列表。

        列名要**去掉引号**再比较：MySQL 会给保留字加反引号（``role`` → 
        ``` `role` ```），PG 用双引号。不归一化的话同一个列在三个方言下会
        被当成三个不同的列名，测试就成了自欺欺人。
        """
        out: dict[str, list[str]] = {}
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            body = ddl.split("(", 1)[1].rsplit(")", 1)[0]
            cols = []
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith(("PRIMARY KEY", "UNIQUE", "CONSTRAINT", "FOREIGN")):
                    continue
                name = line.split()[0].strip('`"')
                cols.append(name)
            out[table.name] = cols
        return out

    pg = _shape(postgresql.dialect())
    my = _shape(mysql.dialect())
    lite = _shape(sqlite.dialect())

    assert pg == my, "PG 与 MySQL 的表/列不一致"
    assert pg == lite, "PG 与 SQLite 的表/列不一致"
    assert len(pg) == 8, f"表数量变了：{sorted(pg)}"


def test_migration_scripts_generate_postgresql_sql():
    """三个迁移脚本要能在 PG 方言下生成可执行的 DDL。

    用 ``alembic upgrade head --sql`` 离线生成——不连库、不建表，只把迁移
    脚本按 PG 方言渲染一遍。这条能抓到「迁移脚本里写了 MySQL 专属类型」
    这类问题：在线模式下要到真建表时才炸，离线模式下当场就看出来。
    """
    env = {**os.environ, "DATABASE_URL": _PG_URL}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"离线生成 PG SQL 失败：\n{proc.stderr}"

    sql = proc.stdout
    assert "CREATE TABLE agent" in sql
    assert "BIGSERIAL" in sql, "迁移脚本没有在 PG 上生成自增主键"
    assert "TIMESTAMP WITHOUT TIME ZONE" in sql
    assert "JSON" in sql
    for marker in _MYSQL_ONLY_MARKERS:
        assert marker not in sql, f"迁移脚本在 PG 上生成了 MySQL 语法：{marker}"


def test_pg_engine_pins_session_timezone_to_utc(monkeypatch):
    """PG 连接必须把会话时区钉成 UTC。

    PG 的 ``now()`` 返回 ``timestamptz``，落进无时区的 ``TIMESTAMP`` 列时按
    **会话时区**折算。会话时区是本地时区的话，同一批数据换个实例写就差了
    8 小时——而且不报错，只是时间线错乱。

    走 libpq 的 ``options`` 启动参数而不是连接后 ``SET``：PG 的裸 ``SET`` 是
    事务性的，连接池归还连接时的 ROLLBACK 会把它撤销，钉了个寂寞。
    """
    monkeypatch.setattr(db_mod.settings, "database_url", _PG_URL)
    kwargs = db_mod._engine_kwargs()

    connect_args = kwargs.get("connect_args", {})
    assert connect_args.get("options") == "-c timezone=UTC", (
        "PG 连接没有下发时区启动参数，时间戳会随会话时区漂"
    )
    # 连接池参数仍按生产配置走
    assert kwargs["pool_size"] == db_mod.settings.db_pool_size
    assert kwargs["max_overflow"] == db_mod.settings.db_max_overflow
    assert kwargs["pool_pre_ping"] is True


def test_pg_url_with_own_options_is_not_duplicated(monkeypatch):
    """用户自己在 URL 里带了 options 时，不能再塞一个同名参数。

    psycopg 收到两个 ``options`` 会直接报 ``TypeError: got multiple values
    for keyword argument 'options'``——连接都建不起来。
    """
    url = _PG_URL + "?options=-c%20timezone%3DAsia%2FShanghai"
    monkeypatch.setattr(db_mod.settings, "database_url", url)
    kwargs = db_mod._engine_kwargs()
    assert "connect_args" not in kwargs, "URL 已带 options，却又下发了一份"


def test_other_dialects_are_untouched(monkeypatch):
    """MySQL 与 SQLite 的引擎参数不能被 PG 的改动波及。"""
    monkeypatch.setattr(db_mod.settings, "database_url", _MYSQL_URL)
    my = db_mod._engine_kwargs()
    assert "connect_args" not in my, "MySQL 不该收到 PG 专用的 connect_args"
    assert my["pool_size"] == db_mod.settings.db_pool_size

    monkeypatch.setattr(db_mod.settings, "database_url", "sqlite+aiosqlite:///./x.db")
    lite = db_mod._engine_kwargs()
    assert lite == {"poolclass": db_mod.NullPool}, "SQLite 必须继续用 NullPool"


def test_pg_dialect_is_loadable_from_the_configured_url():
    """URL 里的驱动名要真的能解析出 PG 方言——拼错会在启动时才炸。"""
    url = sa.engine.make_url(_PG_URL)
    assert url.get_backend_name() == "postgresql"
    assert url.get_driver_name() == "psycopg"


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai"])
def test_pg_timezone_option_is_passed_through(monkeypatch, tz: str):
    """时区值可配：有的机器上就是想让库里存本地时间。"""
    monkeypatch.setattr(db_mod.settings, "database_url", _PG_URL)
    monkeypatch.setattr(db_mod.settings, "db_pg_timezone", tz)
    kwargs = db_mod._engine_kwargs()
    assert kwargs["connect_args"]["options"] == f"-c timezone={tz}"


def test_empty_pg_timezone_disables_the_option(monkeypatch):
    """时区置空就不下发启动参数（服务器时区已经是 UTC 时的省事写法）。"""
    monkeypatch.setattr(db_mod.settings, "database_url", _PG_URL)
    monkeypatch.setattr(db_mod.settings, "db_pg_timezone", "")
    kwargs = db_mod._engine_kwargs()
    assert "connect_args" not in kwargs


def test_pg_timezone_env_name_maps_to_the_setting(monkeypatch):
    """``DB_PG_TIMEZONE`` 这个环境变量名必须真的映射到配置项上。

    名字写错的后果很隐蔽：配置项一直是默认值，功能看着「正常」，用户怎么改
    环境变量都不起作用——这种坑要到部署到别的时区时才暴露。
    """
    from app.config.settings import Settings

    monkeypatch.setenv("DB_PG_TIMEZONE", "Asia/Shanghai")
    s = Settings(_env_file=None)
    assert s.db_pg_timezone == "Asia/Shanghai"
