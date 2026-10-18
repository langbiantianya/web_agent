"""Alembic 迁移：必须能建出与 ORM 一致的全量表结构。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "agent",
    "workflow",
    "conversation",
    "message",
    "run",
    "run_step",
    "run_event",
}


def _alembic_upgrade(db_path: Path) -> None:
    """开子进程跑 ``alembic upgrade head`` 到指定库文件。

    必须用子进程：当前进程里已经建好的 engine 会持有 SQLite 连接，
    alembic 再去改同一个库会撞文件锁。
    """
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _query(db_path: Path, sql: str) -> list[tuple]:
    """用独立的 sqlite3 连接查迁移产物，绕开 ORM 引擎直接看真实库结构。"""
    # sqlite3 的 `with` 只提交事务、**不关连接**，Windows 上会锁住文件，
    # 必须显式 closing。
    with closing(sqlite3.connect(db_path)) as conn:
        return list(conn.execute(sql))


@pytest.fixture
def migrated_db():
    """跑一次 alembic upgrade head，返回库文件路径。"""
    tmpdir = tempfile.mkdtemp(prefix="webagent_migration_")
    db_path = Path(tmpdir) / "migration_test.db"
    try:
        _alembic_upgrade(db_path)
        yield db_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_alembic_creates_all_tables(migrated_db: Path):
    """迁移脚本必须自带完整建表语句，不能依赖「ORM 建过一次」。

    全新库上跑 head 少建一张表，线上首次部署才暴露。
    """
    assert migrated_db.exists()
    names = {r[0] for r in _query(migrated_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names, f"缺少表：{EXPECTED_TABLES - names}"


def test_alembic_schema_matches_orm(migrated_db: Path):
    """迁移产出的表结构要与 ORM metadata 一致，避免「迁移能跑但字段对不上」。

    连**类型**一起比：只比列名的话，把 BIGINT 主键改成不能自增的类型也能蒙混过关。
    """
    from app.models import Base

    type_map = {table: {c.name: c.type for c in Base.metadata.tables[table].columns}
                for table in EXPECTED_TABLES}

    for table in sorted(EXPECTED_TABLES):
        migrated = {
            r[1]: _sqlite_type(r[2])
            for r in _query(migrated_db, f"PRAGMA table_info({table})")
        }
        expected = {
            name: _sqlite_type(str(t).upper()) for name, t in type_map[table].items()
        }
        assert migrated == expected, (
            f"表 {table} 结构不一致："
            f"迁移={migrated} "
            f"ORM={expected}"
        )


def _sqlite_type(decl: str) -> str:
    """把声明归一化成 SQLite 的可读类型，屏蔽 dialect variant 差异。"""
    d = decl.upper()
    if "VARCHAR" in d or "CHAR" in d:
        return "TEXT"
    if "INT" in d:
        return "INTEGER"
    if "JSON" in d or "TEXT" in d:
        return "TEXT"
    if "DATETIME" in d or "TIMESTAMP" in d or "DATE" in d:
        return "DATETIME"
    if "DECIMAL" in d or "NUMERIC" in d or "FLOAT" in d or "REAL" in d:
        return "REAL"
    return d


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("run_event", {"idx_run_created"}),
        ("message", {"idx_conv", "idx_run"}),
        ("run", {"idx_conv_status", "idx_status"}),
        ("conversation", {"idx_user"}),
    ],
)
def test_design_indexes_exist(migrated_db: Path, table: str, expected: set[str]):
    """设计文档 §4.2 里那些支撑查询的索引，一个都不能少。"""
    indexes = {r[1] for r in _query(migrated_db, f"PRAGMA index_list({table})")}
    missing = expected - indexes
    assert not missing, f"{table} 缺少索引：{missing}"


@pytest.mark.parametrize(
    ("table", "columns", "values", "extra"),
    [
        (
            "run_event",
            "(run_id, seq, event_type, payload)",
            "(1, 1, 'token', '{}')",
            "(1, 1, 'token', '{}')",
        ),
        (
            "run_step",
            "(run_id, node_id, agent_code, status)",
            "(1, 'n1', 'a', 'pending')",
            "(1, 'n1', 'b', 'pending')",
        ),
    ],
)
def test_unique_constraints_enforced(
    migrated_db: Path, table: str, columns: str, values: str, extra: str
):
    """uk_run_seq / uk_run_node 必须真的生效——回放幂等全靠它们。

    唯一约束在 SQLite 里表现为匿名 auto-index，名字查不到，因此直接验证行为：
    插入重复键必须报错。
    """
    with closing(sqlite3.connect(migrated_db)) as conn:
        conn.execute(f"INSERT INTO {table} {columns} VALUES {values}")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO {table} {columns} VALUES {extra}")
        conn.rollback()
