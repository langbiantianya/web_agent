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
# 库文件名带上进程号：早先固定名 %TEMP%\web_agent_test.db 时，并行跑两遍 pytest
# 会互相覆盖同一个文件（还会在 import 期 unlink 掉对方正在用的库，Windows 上直接
# 抛 PermissionError），表现为毫无关联的用例随机失败。按进程隔离即可各跑各的。
_TMP_DB = Path(tempfile.gettempdir()) / f"web_agent_test_{os.getpid()}.db"
# SQLite 开了 WAL（见 app/config/db.py），会带出 -wal / -shm 两个副文件。
# 只删主库、留着副文件，下一个进程打开时会把它们当成「已有事务的日志」——
# 症状是莫名其妙的 "database is locked" 或读到不存在的行。一起清掉。
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

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789"
os.environ["DEBUG"] = "false"
# 让批量落库在测试里几乎立即发生，但不关掉批量逻辑本身
os.environ["EVENT_BATCH_SIZE"] = "5"
os.environ["EVENT_FLUSH_INTERVAL"] = "0.02"
os.environ["NODE_RETRY_BASE_DELAY"] = "0.01"
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


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db_file():
    """会话结束后删掉本进程的测试库，不在系统临时目录留垃圾。"""
    yield
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


@pytest.fixture
async def db():
    """请求外的独立 AsyncSession，用于直接断言 DB 事实。"""
    from app.config.db import SessionLocal

    async with SessionLocal() as session:
        yield session
