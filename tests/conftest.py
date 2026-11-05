"""pytest 全局配置。

环境变量必须在 import app.* 之前设定——app.config.settings 在导入期就会读取
DATABASE_URL 并据此创建引擎。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- 测试环境配置（先于任何 app.* 导入）---
# 默认用独立 SQLite；CI/实机验证可显式设置 TEST_DATABASE_URL 切到专用 PG
# 测试库。外部库必须由调用方准备且可清空，conftest 不会自动删除它。
_EXTERNAL_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

# 库文件名带上进程号：早先固定名 %TEMP%\web_agent_test.db 时，并行跑两遍 pytest
# 会互相覆盖同一个文件（还会在 import 期 unlink 掉对方正在用的库，Windows 上直接
# 抛 PermissionError），表现为毫无关联的用例随机失败。按进程隔离即可各跑各的。
_TMP_DB = Path(tempfile.gettempdir()) / f"web_agent_test_{os.getpid()}.db"
# SQLite 开了 WAL（见 app/config/db.py），会带出 -wal / -shm 两个副文件。
# 只删主库、留着副文件，下一个进程打开时会把它们当成「已有事务的日志」——
# 症状是莫名其妙的 "database is locked" 或读到不存在的行。一起清掉。
if not _EXTERNAL_DATABASE_URL:
    for _stale in (
        _TMP_DB,
        _TMP_DB.with_name(_TMP_DB.name + "-wal"),
        _TMP_DB.with_name(_TMP_DB.name + "-shm"),
    ):
        if _stale.exists():
            try:
                _stale.unlink()
            except OSError:  # pragma: no cover - 上一次进程残留且被占用
                pass

os.environ["DATABASE_URL"] = (
    _EXTERNAL_DATABASE_URL or f"sqlite+aiosqlite:///{_TMP_DB.as_posix()}"
)
os.environ["SECRET_KEY"] = "test-secret-key-0123456789"
os.environ["DEBUG"] = "false"
# 管理员门禁的密钥（V4）。测试里要能以管理员身份调写接口；「未配置即拒绝」那条
# fail-closed 的行为由 tests/test_admin.py 用 monkeypatch 单独钉住。
os.environ["ADMIN_KEY"] = "test-admin-key"
# 让批量落库在测试里几乎立即发生，但不关掉批量逻辑本身
os.environ["EVENT_BATCH_SIZE"] = "5"
os.environ["EVENT_FLUSH_INTERVAL"] = "0.02"
os.environ["NODE_RETRY_BASE_DELAY"] = "0.01"
# gRPC 的 C 核心在首次 import 时读取这个变量。测试进程会把 studio 子进程测试与
# 普通 asyncio 用例混跑；不在这里关掉 fork 支持，子进程可能继承其它线程正在使用
# 的 gRPC 状态并在 exec 前 abort。
os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"
# 审批暂停点（V3.1）也按进程隔离：checkpointer 的 SQLite 文件留在项目根目录会
# 变成跨次运行共享的状态，审批用例之间会互相看到对方的暂停点。
os.environ["CHECKPOINT_SQLITE_PATH"] = str(
    Path(tempfile.gettempdir()) / f"web_agent_ckpt_{os.getpid()}.db"
)

# **必须**强制关掉 Redis，而不只是"希望 .env 里没开"。
#
# settings 的 env_file=".env" 是相对路径，pytest 从项目根跑时正好命中开发者本地的
# .env。开着的后果实测过：整个测试套件被切到 Redis 模式，事件写进真实 Redis，
# 而不同用例的 run id 是从 1 开始的小整数，互相撞车，于是出现
# "run 7 的 seq=2 重复写入 Stream" 这类日志，续传/取消/重试三个用例随即随机失败。
# 需要 Redis 的用例（tests/test_redis.py）自己显式建连，不走这个全局开关。
os.environ["REDIS_ENABLED"] = "false"

from app.core.run_hub import get_hub  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    """建表（session 级）。

    刻意做成**同步** fixture：pytest-asyncio 下 session 级 async fixture 会绑定
    到一个独立的 loop，与用例所在 loop 冲突。SQLite 走 NullPool，每次新开连接，
    因此这里建表、那边读写不会串。
    """
    import asyncio

    from app.config.db import engine

    async def _create():
        """建表动作本体；放进 ``engine.begin()`` 让任一张表失败即整批回滚。"""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


@pytest.fixture(scope="module", autouse=True)
def _isolate_module_db(_db_schema):
    """每个测试模块开始时清空业务表。

    单进程跑全量测试时，前一个模块留下的 Skill / Agent 会被下一模块真实应用
    启动审计当成生产配置。按模块隔离与 pytest 文件级进程隔离语义最接近，同时
    保留模块内用例显式共享 fixture 的能力。
    """
    import asyncio

    from app.config.db import engine

    async def _clear():
        """按外键逆序清空全部业务表，建立模块级隔离。"""
        async with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())

    asyncio.run(_clear())
    yield


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db_file():
    """会话结束后删掉本进程的测试库，不在系统临时目录留垃圾。"""
    yield
    if _EXTERNAL_DATABASE_URL:
        return
    try:
        _TMP_DB.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 仍有连接未释放
        pass


@pytest.fixture(autouse=True)
async def _clean_hub():
    """每个用例前后清空 RunHub 状态，避免 seq 计数器与后台任务串味。"""
    yield
    hub = get_hub()
    for run_id in list(hub._states):  # noqa: SLF001
        try:
            await hub.finalize(run_id)
        except Exception:
            pass


@pytest.fixture(autouse=True)
async def _reset_external_db_pool():
    """外部 PostgreSQL 测试时，在每个用例结束后释放连接池。

    pytest-asyncio 默认给每个用例创建一个事件循环，而 SQLAlchemy 的异步连接池
    会缓存连接及其等待队列。池跨事件循环复用时会报
    ``Queue is bound to a different event loop``。生产进程只有一个事件循环，
    不需要这一步；外部数据库测试在用例边界 dispose，既保持池化行为可测，又
    不把测试框架的 loop 隔离泄漏成业务失败。
    """
    yield
    if _EXTERNAL_DATABASE_URL:
        from app.config.db import engine

        await engine.dispose()


@pytest.fixture
async def db():
    """请求外的独立 AsyncSession，用于直接断言 DB 事实。"""
    from app.config.db import SessionLocal

    async with SessionLocal() as session:
        yield session
