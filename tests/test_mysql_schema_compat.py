"""MySQL 特有的结构约束（行大小上限）在模型与迁移两侧都成立。

为什么要单开一个文件：这条约束**只在 MySQL 上**才爆炸。SQLite 与 PostgreSQL
都不按字节计行长，本地把测试跑绿一百遍也发现不了——第一次真正部署到 MySQL
时，`mcp_server` 的两个 ``VARCHAR(8192)`` 直接让迁移报
``1118 Row size too large``，服务起不来，而且因为 MySQL 的 DDL 不是事务性的，
失败的半截结构留在库里，重试还会变成 ``Duplicate column name``。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import postgresql as pg_dialect

from app.models import Base
from app.models.base import long_text

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0012 = ROOT / "alembic" / "versions" / "0012_mcp_oauth_tenant.py"

# MySQL / InnoDB 的行大小上限（不含 TEXT/BLOB，它们存在行外）。
MYSQL_ROW_LIMIT = 65535
# utf8mb4 下每字符最多 4 字节，DDL 按最大字节数预留。
BYTES_PER_CHAR = 4


def _mysql_row_bytes(table: sa.Table) -> int:
    """按 MySQL 实际会渲染出的 DDL 估算一张表的行内字节数。

    必须看**渲染结果**而不是 ``isinstance(column.type, sa.String)``：
    ``long_text()`` 的基类型仍是 ``String``，在 MySQL 上才变成 ``TEXT``（行外
    存储、不计入行大小）。只看基类型会把它当 VARCHAR 算，得出错误的结论。
    """
    total = 0
    for column in table.columns:
        rendered = column.type.compile(dialect=mysql_dialect.dialect())
        if rendered.startswith("VARCHAR("):
            length = int(rendered[len("VARCHAR(") : -1])
            total += length * BYTES_PER_CHAR
    return total


def test_every_model_table_fits_mysql_row_limit():
    """每张表的 VARCHAR 行内字节数都要在 65535 以内，否则 MySQL 建表就失败。"""
    over_limit = {
        table.name: _mysql_row_bytes(table)
        for table in Base.metadata.sorted_tables
        if _mysql_row_bytes(table) > MYSQL_ROW_LIMIT
    }
    assert not over_limit, (
        "这些表在 MySQL（utf8mb4）上会报 1118 Row size too large，"
        f"请把长字段改用 app.models.base.long_text：{over_limit}"
    )


def test_long_text_is_text_on_mysql_and_varchar_elsewhere():
    """变体只影响 MySQL：其它方言必须还是原来的 VARCHAR(length)。"""
    column_type = long_text(8192)
    assert column_type.compile(dialect=mysql_dialect.dialect()) == "TEXT"
    assert column_type.compile(dialect=pg_dialect.dialect()) == "VARCHAR(8192)"


def _load_migration_0012():
    spec = importlib.util.spec_from_file_location("migration_0012", MIGRATION_0012)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RecordingBatch:
    """替身：记录迁移真正要加的列，不碰任何数据库。"""

    def __init__(self, sink: list[sa.Column]) -> None:
        self._sink = sink

    def __enter__(self) -> _RecordingBatch:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def add_column(self, column: sa.Column) -> None:
        self._sink.append(column)

    def drop_index(self, name: str) -> None:
        return None

    def create_index(self, name: str, columns: list[str], **kwargs: object) -> None:
        return None


def test_migration_0012_adds_long_ciphers_as_text_on_mysql(monkeypatch):
    """跑一遍 0012 的 upgrade，确认两个长密文列在 MySQL 上落成 TEXT。

    这是那次部署事故的**直接回归用例**：改回 ``sa.String(length=8192)``
    会让这条断言立刻变红。
    """
    module = _load_migration_0012()
    added: list[sa.Column] = []
    monkeypatch.setattr(
        module.op,
        "batch_alter_table",
        lambda table_name, **kwargs: _RecordingBatch(added),
    )

    module.upgrade()

    by_name = {column.name: column for column in added}
    for column_name in (
        "oauth_access_token_cipher",
        "oauth_refresh_token_cipher",
    ):
        assert column_name in by_name, f"0012 没有新增 {column_name}"
        rendered = by_name[column_name].type.compile(dialect=mysql_dialect.dialect())
        assert rendered == "TEXT", f"{column_name} 在 MySQL 上仍是 {rendered}"

    # 短字段不该跟着变：它们的行内占用可控，保持 VARCHAR 更省事。
    assert (
        by_name["oauth_client_secret_cipher"].type.compile(
            dialect=mysql_dialect.dialect()
        )
        == "VARCHAR(4096)"
    )
