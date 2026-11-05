"""Alembic 迁移：必须能建出与 ORM 一致的全量表结构。"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

VERSIONS_DIR = ROOT / "alembic" / "versions"

# MySQL 不允许这几类列带字面量默认值（报错 1101）：
# "BLOB, TEXT, GEOMETRY or JSON column 'xxx' can't have a default value"。
# VARCHAR / INTEGER 等普通类型带默认值是允许的，不在禁列范围内。
MYSQL_NO_DEFAULT_TYPES = {
    "TEXT",
    "UNICODETEXT",
    "LARGEBINARY",
    "JSON",
    "BLOB",
    "LONGTEXT",
    "MEDIUMTEXT",
    "TINYTEXT",
    "LONGBLOB",
    "MEDIUMBLOB",
    "TINYBLOB",
    "JSONB",
    "GEOMETRY",
}

EXPECTED_TABLES = {
    "agent",
    "workflow",
    "conversation",
    "message",
    "run",
    "run_step",
    "run_event",
    "run_approval",
    "run_approval_vote",
    "skill",
    "skill_resource",
    "skill_revision",
    "skill_revision_resource",
    "mcp_server",
    "mcp_tool",
    "llm_endpoint",
    "tool_call_audit",
    "admin_user",
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


def test_primary_keys_are_rowid_aliases(migrated_db: Path):
    """每个表的主键列都必须声明成 ``INTEGER PRIMARY KEY``。

    SQLite 里只有 ``INTEGER PRIMARY KEY`` 才是 rowid 别名、才会自增。写成
    ``BIGINT`` 的主键不会自增，插入时必须自己给 id，实际报错是
    "NOT NULL constraint failed: xxx.id"。

    这类问题在 ORM 建表时不会出现：模型用的是
    ``BigInteger().with_variant(Integer, 'sqlite')``（见 :data:`app.models.base.PK`），
    create_all 建出来的就是 INTEGER。**只有走迁移建出来的库**会出问题——而测试
    套件里的库恰恰是 create_all 建的，所以这条断言盯的必须是**迁移产物**。

    上面那条「结构与 ORM 一致」的断言抓不到它：那里的类型归一化把 BIGINT 和
    INTEGER 都算成 "INTEGER"，主键到底能不能自增被抹平了。
    """
    for table in sorted(EXPECTED_TABLES):
        pk = [
            r for r in _query(migrated_db, f"PRAGMA table_info({table})")
            if r[5]
        ]
        assert len(pk) == 1, f"{table} 的主键列数量是 {len(pk)}，应为 1"
        name, decl = pk[0][1], pk[0][2]
        assert decl.upper() == "INTEGER", (
            f"{table}.{name} 声明成 {decl}——SQLite 上只有 INTEGER PRIMARY KEY "
            f"才自增，写成 BIGINT 会报 NOT NULL constraint failed"
        )


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("run_event", {"idx_run_created"}),
        ("message", {"idx_conv", "idx_run"}),
        ("run", {"idx_conv_status", "idx_status"}),
        ("conversation", {"idx_user"}),
        ("llm_endpoint", {"idx_llm_endpoint_enabled_priority"}),
        (
            "tool_call_audit",
            {
                "idx_tool_audit_tenant_created",
                "idx_tool_audit_run_created",
                "idx_tool_audit_expires",
            },
        ),
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


def _dotted_name(node: ast.AST) -> str | None:
    """把 ``sa.Text`` / ``mysql.LONGTEXT`` 这样的属性链还原成末段名字。

    只取末段（``Text``）就够了：判定禁列只看类型本身，不看它挂在哪个命名空间下。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _column_type_name(node: ast.AST) -> str | None:
    """取列的类型名，剥掉 ``with_variant`` 这层方言包装。

    ``sa.Text().with_variant(mysql.LONGTEXT(), "mysql")`` 的类型仍然是 Text 系，
    一样不能让 MySQL 接受默认值，所以必须往里剥。
    """
    if isinstance(node, ast.Call):
        func = node.func
        # sa.Text() / mysql.LONGTEXT()
        if isinstance(func, ast.Attribute):
            if func.attr == "with_variant" and func.value is not None:
                return _column_type_name(func.value)
            return func.attr
        # 直接传类型类：sa.Text
        return _dotted_name(func)
    # 直接传类型：sa.Text
    return _dotted_name(node)


def test_no_text_or_json_column_has_server_default():
    """迁移里禁止出现「TEXT/JSON/BLOB 类列带字面量默认值」。

    MySQL 会直接拒绝这种 DDL：

    ``(1101, "BLOB, TEXT, GEOMETRY or JSON column 'prompt' can't have a
    default value")``

    这个坑在本地测不出来——SQLite 和 PostgreSQL 都接受 TEXT 默认值，测试套件
    用的又是 SQLite，所以它只在真 MySQL 部署时才炸，而且炸在建表阶段，容器
    陷入 CrashLoop。开发期唯一挡得住它的办法就是静态扫迁移脚本。

    带默认值本身不是错（VARCHAR/INTEGER 列完全合法，比如 status 的
    ``server_default="pending"``），只有 MySQL 禁列类型才是问题，所以按类型判。
    """
    offenders: list[str] = []
    for py_file in sorted(VERSIONS_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "Column"):
                continue
            # sa.Column(name, type, ...) —— 类型是第 2 个位置参数
            if len(node.args) < 2:
                continue
            type_name = _column_type_name(node.args[1])
            if type_name is None or type_name.upper() not in MYSQL_NO_DEFAULT_TYPES:
                continue
            has_default = any(kw.arg == "server_default" for kw in node.keywords)
            if has_default:
                column = node.args[0]
                col_name = (
                    column.value if isinstance(column, ast.Constant) else "<动态列名>"
                )
                offenders.append(f"{py_file.name}:{node.lineno} 列 {col_name} ({type_name})")

    assert not offenders, (
        "MySQL 不允许 TEXT/JSON/BLOB 列带默认值（1101），请去掉这些列的 "
        "server_default，改用 ORM 的 Python 侧 default：\n  " + "\n  ".join(offenders)
    )


# 只用来确定方言，**不建立任何连接**：alembic 的离线模式（``--sql``）只渲染
# DDL，不连库，所以这里不需要真的 MySQL。
MYSQL_DIALECT_URL = "mysql+asyncmy://render:render@127.0.0.1:3306/render_only"

# MySQL 拒绝的「函数默认值没括号」：DEFAULT now() 是非法的，
# 只有 DEFAULT (now()) 才是合法的表达式默认值（MySQL 8.0.13+）。
BARE_FUNC_DEFAULT_RE = re.compile(r"DEFAULT\s+(?!\()[A-Za-z_]+\s*\(")

# 列定义行里出现「文本/二进制/JSON 类型 + DEFAULT」就是 1101。
# 渲染出来的 DDL 每个列定义占一行，所以按行匹配即可（``[^\n]*`` 不跨行）。
TEXT_TYPE_DEFAULT_RE = re.compile(
    r"^\s*\w+\s+(?:TINY|MEDIUM|LONG)?(?:TEXT|BLOB)\b[^\n]*\bDEFAULT\b",
    re.MULTILINE | re.IGNORECASE,
)
JSON_TYPE_DEFAULT_RE = re.compile(
    r"^\s*\w+\s+(?:JSON|JSONB)\b[^\n]*\bDEFAULT\b",
    re.MULTILINE | re.IGNORECASE,
)


def _render_mysql_ddl() -> str:
    """离线渲染「升级到 head」的完整 MySQL DDL。

    用 alembic 的 ``--sql``（离线）模式：不连数据库，只把每个迁移会发出的
    SQL 打出来。这样就能在**没有 MySQL 的机器上**检查「发给 MySQL 的 DDL 是否
    会被服务端拒绝」——这正是本地测试唯一缺的那一环。
    """
    env = {**os.environ, "DATABASE_URL": MYSQL_DIALECT_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_mysql_ddl_uses_only_defaults_mysql_accepts():
    """渲染出来的 MySQL DDL 里不能有服务端会拒绝的默认值写法。

    一条硬规则，是真 MySQL 上撞出来的：

    **TEXT/BLOB/JSON 列不能带默认值**——服务端报 1101
    "BLOB, TEXT, GEOMETRY or JSON column 'xxx' can't have a default value"。

    这类错误在本地测不出来——SQLite 和 PostgreSQL 都接受 TEXT 默认值，测试
    套件用的又是 SQLite，所以它只在真 MySQL 部署时才炸，而且炸在建表阶段，
    容器陷入 CrashLoop。

    （函数默认值必须带括号那条由下面那条更精确的测试负责。）
    """
    ddl = _render_mysql_ddl()
    assert ddl.strip(), "离线渲染出来的 DDL 是空的，说明 alembic 离线模式没跑起来"

    problems = [
        f"TEXT/BLOB 列带默认值：{m.group(0).strip()}"
        for m in TEXT_TYPE_DEFAULT_RE.finditer(ddl)
    ]
    problems += [
        f"JSON 列带默认值：{m.group(0).strip()}"
        for m in JSON_TYPE_DEFAULT_RE.finditer(ddl)
    ]

    assert not problems, (
        "渲染出的 MySQL DDL 里有服务端会拒绝的默认值写法（1101）：\n  "
        + "\n  ".join(problems)
    )


def test_mysql_ddl_keeps_function_defaults_parenthesized():
    """函数默认值在 DDL 里必须带括号——逐个点名检查，别只靠正则。

    这是上一条测试的**精确版**：只认 ``DEFAULT (now())`` 这种带括号的写法，
    其余的函数默认值（``now()`` / ``current_timestamp()`` / ``uuid()`` …）
    一律算违规。MySQL 8.0.13 之后才支持表达式默认值，且语法要求带括号。
    """
    ddl = _render_mysql_ddl()
    offenders = [
        line.strip()
        for line in ddl.splitlines()
        if BARE_FUNC_DEFAULT_RE.search(line)
    ]
    assert not offenders, (
        "MySQL 的表达式默认值必须带括号（DEFAULT (now()) 合法，DEFAULT now() "
        "报 1067）：\n  " + "\n  ".join(offenders)
    )
